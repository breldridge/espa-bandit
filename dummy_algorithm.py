# This is a test dummy algorithm to get the opportunity cost curves
from ortools.linear_solver import pywraplp
import offer_utils as ou
import pandas as pd
import numpy as np
import argparse
import json
import datetime
import logging
from itertools import accumulate

# Standard battery parameters
socmax = 608
socmin = 128
chmax = 125
dcmax = 125
efficiency = 0.892
duration_minutes = 5

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return json.JSONEncoder.default(self, obj)


class Agent():
    '''
    Agent is re-initialized every time the WEASLE Platform calls market_participant.py
    Input: time_step, market_data, and resource_data are input arguments from the script call
    Additional input data must be saved to disc and reloaded each time Agent is created (e.g., to facilitate Agent persistence)
    Output:
    - make_me_an_offer() reads the market type and saves to disc a JSON file containing offer data
    '''

    def __init__(self, time_step, market_info, resource_info, name='bandit'):
        # Data input from WEASLE
        self.step = time_step
        self.market = market_info
        self.resource = resource_info
        self.rid = resource_info['rid']

        self.duration_minutes = duration_minutes

        # Standard battery parameters
        self.socmax = socmax
        self.socmin = socmin
        self.chmax = chmax
        self.dcmax = dcmax
        self.efficiency = efficiency

        # Configurable options
        self.price_ceiling = 999
        self.price_floor = 0

        # Add the offer binner
        self.binner = ou.Binner(output_type='lists')

        # logger
        self._add_logger(f'{name}_log')

        # self._prev_dam_file = 'prev_day_ahead_market'
        # self.save_from_previous()

    def make_me_an_offer(self):
        # Read in information from the market
        market_type = self.market["market_type"]
        if 'DAM' in market_type:
            self.logger.info("generating DA offer...")
            # offer = self._day_ahead_offer()
            offer = ou.compute_offers(self.resource, self.market['timestamps'], None, None)
        elif 'RTM' in market_type:
            self.logger.info(f'generating RT offer for t={self.market["timestamps"][0]}...')
            offer = self._real_time_offer()
        else:
            raise ValueError(f"Unable to find offer function for market_type={market_type}")

        # self._decrease_charging_offers(offer, 1)
        self._increase_discharging_offers(offer, 1)

        # Then save the result
        self._save_json(offer, f'offer_{self.step}.json')

    def save_from_previous(self):
        # if the current market type is DAM, then we need to save it in order to run RTM
        if 'DAM' in self.market["market_type"]:
            self._save_json(self.market['previous'], self._prev_dam_file)

    def _add_logger(self, name):

        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)

        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(formatter)

        self.logger.addHandler(console_handler)

    def _day_ahead_offer(self):
        # Make the offer curves and unload into arrays
        type = self.market['market_type']
        # bus = self.resource['bus'] #TODO: switch when this is updated
        bus = 'NEVP'
        prices = self.market["previous"][type]["prices"]["EN"][bus]
        self._calculate_offer_curve(prices)
        # self._descretize_offer_curves()
        self._format_offer_curves()
        self.logger.debug("ready to save DA offer")

        return self.formatted_offer

    def _offer_to_dicts(self, mq_list=None, mc_list=None):

        if not mq_list and not mc_list:
            zeros = {t: 0 for t in self.market['timestamps']}
            return zeros, zeros

        mq_dict = {}
        mc_dict = {}
        for i, time in enumerate(self.market['timestamps']):
            mc = mc_list[i]
            mq = mq_list[i]
            if isinstance(mc, (int,float)) and isinstance(mq, (int,float)):
                self.logger.debug(f"{time} offer tuple ({mq},${mc}) does not require binning.")
                mq_dict[time] = mq
                mc_dict[time] = mc
            elif isinstance(mc, list) and isinstance(mq, list):
                assert len(mc) == len(mq), f"charge mc and mq have different length, period {time}"
                lmc = len(mc)
                offer = self.binner.collate(mq, mc)
                self.logger.debug(f"{time} offer list length {lmc} --> {len(offer[0])} after binning.")
                mq_dict[time] = offer[0]
                mc_dict[time] = offer[1]
            else:
                raise TypeError(f"unsupported charge offer types: mc is {type(mc)}, mq is {type(mq)}")
        return mq_dict, mc_dict

    def _format_offer_curves(self):

        # Convert the offer curves to timestamp:offer_value dictionaries
        block_ch_mq, block_ch_mc = self._offer_to_dicts(self.charge_mq, self.charge_mc)
        block_dc_mq, block_dc_mc = self._offer_to_dicts(self.discharge_mq, self. discharge_mc)
        block_soc_mq, block_soc_mc = self._offer_to_dicts()

        # estimate initial SoC for tomorrow's DAM
        t_init = datetime.datetime.strptime(self.market['timestamps'][0],'%Y%m%d%H%M')
        # t_now = datetime.datetime.strptime(self.market['current_time'],'%Y%m%d%H%M') #TODO: switch back once above in included in market_data
        t_now = datetime.datetime.strptime(self.market['uid'][5:],'%Y%m%d%H%M')
        t_init = t_init.strftime('%Y%m%d%H%M')
        t_now = t_now.strftime('%Y%m%d%H%M')
        if self.resource['schedule'].keys():
            schedule = self.resource['schedule'][self.rid]['EN']
            schedule_to_tomorrow = [q for t,q in schedule if t_now <= t < t_init]   # these may be misordered but that is OK
            schedule_to_tomorrow = self._process_efficiency(schedule_to_tomorrow)
            soc_estimate = self.resource['status'][self.rid]['soc'] - sum(schedule_to_tomorrow) * self.duration_minutes / 60
            dispatch_estimate = self.resource['schedule'][self.rid]['EN'][t_init]
        else:
            soc_estimate = self.resource['status'][self.rid]['soc']
            dispatch_estimate = 0
        soc_estimate = min(self.socmax, max(soc_estimate, self.socmin))


        # Package the dictionaries into an output formatted dictionary
        offer_out_dict = {self.rid: {}}
        offer_out_dict[self.rid] = {"block_ch_mc": block_ch_mc, "block_ch_mq": block_ch_mq, "block_dc_mc": block_dc_mc,
                               "block_dc_mq": block_dc_mq, "block_soc_mc": block_soc_mc, "block_soc_mq": block_soc_mq}
        offer_out_dict[self.rid].update(self._default_reserve_offer())
        offer_out_dict[self.rid].update(self._default_dispatch_capacity())
        offer_out_dict[self.rid].update(self._default_offer_constants(soc_begin=soc_estimate, init_en=dispatch_estimate))

        self.formatted_offer = offer_out_dict

    def _descretize_offer_curves(self):
        charge_offer = self.binner.collate(self.charge_mq, self.charge_mc)
        discharge_offer = self.binner.collate(self.discharge_mq, self.discharge_mc)
        self.charge_mq = charge_offer[0]
        self.charge_mc = charge_offer[1]
        self.discharge_mq = discharge_offer[0]
        self.discharge_mc = discharge_offer[1]

    def _process_efficiency(self, data:list):
        processed_data = []
        for num in data:
            if num < 0:
                processed_data.append(num * self.efficiency)
            else:
                processed_data.append(num)
        return processed_data

    def _real_time_offer(self):
        initial_soc = self.resource["status"][self.rid]["soc"]
        soc_available = initial_soc - self.socmin
        soc_headroom = self.socmax - initial_soc
        best_dc_price = self.price_floor
        best_ch_price = self.price_ceiling
        block_dc_mc = {}
        block_dc_mq = {}
        block_ch_mc = {}
        block_ch_mq = {}
        block_soc_mc = {}
        block_soc_mq = {}

        t_end = max(self.market['timestamps'])
        self.logger.debug(f'Last timestamp set to {t_end}')
        for t in self.market['timestamps']:
            block_ch_mq[t] = []
            block_ch_mc[t] = []
            block_dc_mq[t] = []
            block_dc_mc[t] = []
            if t not in self.resource['ledger'][self.rid]['EN'].keys():
                block_ch_mq[t].append(self.chmax)
                block_ch_mc[t].append(0)
                block_dc_mq[t].append(self.dcmax)
                block_dc_mc[t].append(0)
                self.logger.debug(f"no ledger entry in period {t}.")
                continue

            # use these 'zero' offers
            block_ch_mq[t].append(self.chmax)
            block_ch_mc[t].append(0)
            block_dc_mq[t].append(self.dcmax)
            block_dc_mc[t].append(0)

            en_ledger = self.resource['ledger'][self.rid]['EN'][t]
            self.logger.debug(f"generating energy ledger in period {t}. number of orders={len(en_ledger)}")

            # determine best prices and available SoC
            for i,order in enumerate(en_ledger):
                self.logger.debug(f'{t}: looking into order {i+1}: {order}')
                mq, mc = order
                if -soc_headroom <= mq * 5/60 < 0:
                    soc_available += mq * self.efficiency * 5/60
                    soc_headroom -= mq * self.efficiency * 5/60
                    # block_ch_mq[t].append(-mq)
                    # block_ch_mc[t].append(mc)
                    # self.logger.info(f"added ({-mq},${mc}) to charge cost curve, best price is {best_ch_price}")
                    best_ch_price = min(best_ch_price, mc)
                elif 0 < mq * 5/60 <= soc_available:
                    soc_available -= mq * 5/60
                    soc_headroom += mq * 5/60
                    # block_dc_mq[t].append(mq)
                    # block_dc_mc[t].append(mc)
                    # self.logger.info(f"added ({mq},${mc}) to discharge cost curve")
                    best_dc_price = max(best_dc_price, mc)
                elif mq * 5/60 < -soc_headroom:
                    self.logger.warning(f"Period {t}: Scheduled charge exceeds SoC headroom. Setting headroom to zero and available to max. ")
                    soc_headroom = 0
                    soc_available = self.socmax - self.socmin
                    best_ch_price = min(best_ch_price, mc)
                elif mq * 5/60 >soc_available:
                    self.logger.warning(f"Period {t}: Scheduled discharge exceeds SoC available. Setting available to zero and headroom to max. ")
                    soc_headroom = self.socmax - self.socmin
                    soc_available = 0
                    best_dc_price = max(best_dc_price, mc)
                else:
                    self.logger.warning("A mutually exhaustive set of cases was found to be faulty. Recommend further investigation.")

                if soc_available < 0:
                    self.logger.warning(f"Scheduled energy flows result in SoC below min in {t}")
                if soc_headroom < 0:
                    self.logger.warning(f"Scheduled energy flows result in SoC above max in {t}")

        # valuation of post-market SoC
        post_market_ledger = {t: order for t, order in self.resource['ledger'][self.rid]['EN'].items() if t > t_end}
        self.logger.debug(f"ledger includes {len(post_market_ledger)} additional time periods")
        for t, order in post_market_ledger.items():
            for mq,mc in order:
                best_ch_price = min(best_ch_price, mc)
                best_dc_price = max(best_dc_price, mc)
        self.logger.debug(f'best charging price: {best_ch_price}')
        self.logger.debug(f'best discharging price: {best_dc_price}')

        for t in self.market['timestamps']:
            # add remaining discharge capacity at max known price
            dc_capacity = self.dcmax - sum(block_dc_mq[t])
            if dc_capacity > 1e-2:
                block_dc_mq[t].append(dc_capacity)
                block_dc_mc[t].append(best_dc_price)
                self.logger.debug(f"added {dc_capacity} discharging capacity in time {t}")
            # add remaining discharge capacity at min known price
            ch_capacity = self.chmax - sum(block_ch_mq[t])
            if ch_capacity > 1e-2:
                block_ch_mq[t].append(ch_capacity)
                block_ch_mc[t].append(best_ch_price)
                self.logger.debug(f"added {ch_capacity} charging capacity in time {t}")


        # valuation of post-horizon SoC
        post_market_list = [tup for t, sublist in post_market_ledger.items() for tup in sublist]
        post_market_sorted = sorted(post_market_list, key=lambda tup:tup[1], reverse=True)
        message = ''
        for i,tup in enumerate(post_market_sorted):
            message += f'\t{i}: {tup}\n'
        self.logger.debug(f'Remaining orders:\n'+message)
        soc_mq = []
        soc_mc = []
        remaining_capacity = soc_available
        self.logger.info(f"{remaining_capacity} MWh available at end of horizon. Allocating value...")
        for mq, mc in post_market_sorted:
            # if discharging in the future
            if 0 < mq * 5/60 <= remaining_capacity:
                self.logger.debug(f"post horizon SoC quantity {mq} valued at {mc}.")
                remaining_capacity -= mq * 5/60
                soc_mq.append(mq)
                soc_mc.append(mc)
            # discharge exhausts remaining capacity
            elif 0 < remaining_capacity < mq * 5/60:
                remaining_capacity = 0
                soc_mq.append(remaining_capacity)
                soc_mc.append(mc)
            # skip if no capacity is left
            elif remaining_capacity < 1e-2:
                break
            # charging
            else:
                pass

        if remaining_capacity > 1e-2:
            soc_mq.append(remaining_capacity)
            soc_mc.append(self.price_ceiling)
        soc_mq.append(soc_headroom)
        soc_mc.append(0)

        # collate into bins
        self.logger.info(f"SoC offer has {len(soc_mq)} elements")
        self.logger.debug(f"raw soc quantities are {soc_mq}")
        self.logger.debug(f"raw soc prices are {soc_mc}")
        soc_offer = self.binner.collate(soc_mq, soc_mc)
        block_soc_mq[t_end] = soc_offer[0]
        block_soc_mc[t_end] = soc_offer[1]
        self.logger.info(f"Binned SoC offer has {len(soc_offer[0])} elements")
        self.logger.debug(f"binned soc quantities are {soc_offer[0]}")
        self.logger.debug(f"binned soc prices are {soc_offer[1]}")

        # Package the dictionaries into an output formatted dictionary
        offer_out_dict = {self.rid: {}}
        offer_out_dict[self.rid] = {"block_ch_mc": block_ch_mc, "block_ch_mq": block_ch_mq, "block_dc_mc": block_dc_mc,
                               "block_dc_mq": block_dc_mq, "block_soc_mc": block_soc_mc, "block_soc_mq": block_soc_mq}
        offer_out_dict[self.rid].update(self._default_reserve_offer())
        offer_out_dict[self.rid].update(self._default_dispatch_capacity())
        offer_out_dict[self.rid].update(self._default_offer_constants(bid_soc=True))

        return offer_out_dict

    def _decrease_charging_offers(self, offer, adjustment):
        old_block = offer[self.rid]['block_ch_mc']
        new_block = {}
        self.logger.debug(f"old block is {old_block}")
        for t, old_offer in old_block.items():
            if isinstance(old_offer, int):
                new_block[t] = float(old_offer - adjustment)
            elif isinstance(old_offer, list):
                new_block[t] = [mc - adjustment for mc in old_offer]
            else:
                raise TypeError(f'charge block type is unsupported. type={type(old_block)}')
        self.logger.debug(f"new block is {new_block}")
        self.logger.info(f'decreasing charging offers by ${adjustment}')
        offer[self.rid]['block_ch_mc'] = new_block

    def _increase_discharging_offers(self, offer, adjustment):
        old_block = offer[self.rid]['block_dc_mc']
        new_block = {}
        self.logger.debug(f"old block is {old_block}")
        for t, old_offer in old_block.items():
            if isinstance(old_offer, int):
                new_block[t] = float(old_offer + adjustment)
            elif isinstance(old_offer, list):
                new_block[t] = [mc + adjustment for mc in old_offer]
            else:
                raise TypeError(f'discharge block type is unsupported. type={type(old_block)}')
        self.logger.debug(f"new block is {new_block}")
        self.logger.info(f'increasing discharging offers by ${adjustment}')
        offer[self.rid]['block_dc_mc'] = new_block

    def _default_reserve_offer(self):
        reg = ['cost_rgu', 'cost_rgd', 'cost_spr', 'cost_nsp']
        res_dict = {}
        for r in reg:
            res_dict[r] = {t: 0 for t in self.market['timestamps']}
        return res_dict

    def _default_dispatch_capacity(self):
        max_dict = {}
        max_dict['chmax'] = {t: self.chmax for t in self.market['timestamps']}
        max_dict['dcmax'] = {t: self.dcmax for t in self.market['timestamps']}
        return max_dict

    def _default_offer_constants(self, **options):
        constants = {}
        constants['soc_begin'] = self.resource['status'][self.rid]['soc']
        constants['init_en'] = self.resource['status'][self.rid]['dispatch']
        constants['init_status'] = 1
        constants['ramp_dn'] = 9999
        constants['ramp_up'] = 9999
        constants['socmax'] = self.socmax
        constants['socmin'] = self.socmin
        constants['eff_ch'] = self.efficiency
        constants['eff_dc'] = 1.0
        constants['soc_end'] = self.socmin
        constants['bid_soc'] = False

        constants.update(options)

        return constants

    def _load_dam_prices_times(self):
        now = self.market['timestamps'][0]
        hour_beginning = now[:10] + '00'
        type = self.market['market_type']
        bus = self.resource['bus']
        if hour_beginning in self.market['previous'][type]['timestamp']:
            prices = self.market['previous'][type]['EN'][bus]
            times = self.market['previous'][type]['timestamp']
        else:
            with open(self._prev_dam_file, "r") as file:
                prices = json.load(file)
                times = [key for key in prices.keys()]
                prices = [value for value in prices.values()]
        return prices, times

    def _save_json(self, save_dict, filename=None):
        # Save as json file in the current directory with name offer_{time_step}.json
        if filename is None:
            filename =f'offer_{self.step}.json'
        self.logger.info(f"saving {filename}")
        with open(filename, 'w') as f:
            json.dump(save_dict, f, indent=4, cls=NpEncoder)

    def _calculate_opportunity_costs(self, prices, charge_mq, discharge_mq):

        # combine the charge/discharge list
        combined_list = [dis - ch for ch, dis in zip(charge_mq, discharge_mq)]
        time = self.market['timestamps']
        schedule = dict(zip(time, combined_list))

        # finding the index for first charge and last discharge
        t1_ch = next((index for index, key in enumerate(schedule) if schedule[key] < 0), None)
        t_last_dis = next((i for i in range(len(combined_list) - 1, -1, -1) if combined_list[i] > 0), None)
        assert isinstance(t1_ch, int), "t1_ch is not an int"
        assert isinstance(t_last_dis, int), "t_last_dis is not an int"

        # create two list for charging/discharging opportunity costs
        charge_list = []
        discharge_list = []

        # opportunity_costs = pd.DataFrame(None, index=range(len(prices)), columns=['Time', 'charge cost', 'disch cost'])
        # soc = pd.DataFrame(None, index=range(len(prices) + 1), columns=['Time', 'SOC'])

        for index, key in enumerate(schedule):
            i = index
            value = schedule[key]

            # charging
            if value < 0:
                oc_ch, oc_dis = self._calc_oc_charge(combined_list, prices, i)
            # discharging
            elif value > 0:
                oc_ch, oc_dis = self._calc_oc_discharge(combined_list, prices, i)
            else:
                # before first charge
                if i < t1_ch:
                    oc_ch, oc_dis = self._calc_oc_before_first_charge(prices, t1_ch, i)
                # after the last discharge
                elif i > t_last_dis:
                    oc_ch, oc_dis = self._calc_oc_after_last_discharge(prices, t_last_dis, i)
                # between cycles
                else:
                    oc_ch, oc_dis = self._calc_oc_between_cycles(combined_list, prices, i)

            # save to list
            self.logger.debug(f"price... \ttime {i} \tlmp {prices[i]} \t ch {oc_ch} \t dc {oc_dis}")
            charge_list.append(oc_ch)
            discharge_list.append(oc_dis)

        assert sum(abs(c) for c in charge_list) > 0, "calc_oc: charge list has no values"
        assert sum(abs(d) for d in discharge_list) > 0, "calc_oc: discharge list has no values"

        return charge_list, discharge_list

    def _calculate_offer_curve(self, prices):

        # marginal cost comes from opportunity cost calculation
        charge_mq, discharge_mq = self._scheduler(prices)
        charge_mc, discharge_mc = self._calculate_opportunity_costs(prices, charge_mq, discharge_mq)
        # self.charge_mc = oc['charge cost'].values
        # self.discharge_mc = oc['disch cost'].values
        self.charge_mc = charge_mc
        self.discharge_mc = discharge_mc

        # marginal quantities from scheduler values
        self.charge_mq = charge_mq
        self.discharge_mq = discharge_mq

    def _calc_oc_charge(self, combined_list, prices, idx):
        # opportunity cost during scheduled charge
        # 'j' is the next discharging period or end-of-horizon
        j = None
        if idx < len(combined_list) - 1:
            j = idx + 1 + next(index for index, value in enumerate(combined_list[idx + 1:]) if value > 0)
        if not j:
            j = min(idx + 1, len(prices) - 1) # something to ensure that j is in the range of the lists
        # j = idx + 1 + next((index for index, value in enumerate(combined_list[idx + 1:]) if value > 0), None)

        if idx == 0:
            oc_ch = min(min(prices[1:j]), self.efficiency * prices[j])
            oc_dis = oc_ch + 0.01
            return oc_ch, oc_dis
        # arr1: next best purchase price
        # arr2: next best sell price
        elif j == idx + 1:                  # planning to discharge next period
            arr1 = min(prices[0:idx])
            arr2 = 0
        elif j == idx + 2:                  # planning to discharge right after next period
            arr1 = min(prices[0:idx])
            arr2 = prices[j - 1]
        else:                               # planning to discharge after next period (j is defined greater than idx)
            arr1 = min(prices[0:idx])
            arr2 = max(prices[idx:j+1])

        oc_ch = min(prices[:idx] + prices[idx+1:]) * self.efficiency
        oc_dis = arr1 * self.efficiency + arr2 - prices[idx]
        return oc_ch, oc_dis

    def _calc_oc_discharge(self, combined_list, prices, idx):
        # opportunity cost during scheduled discharge

        # 'j' is the last charging period or the beginning of horizon
        j = max((index for index, value in enumerate(combined_list[:idx-1]) if value < 0), default=None)
        if not j:
            j = 0

        # arr1: next best sell price
        if idx == len(prices) - 1:      # currently in last period
            arr1 = 0
        else:                           # any earlier period
            arr1 = max(prices[idx+1:])

        # arr2: next best purchase price
        if j == idx - 1:                # last charge was last period
            arr2 = min(prices[j], 0)
        elif j == idx - 2:              # last charge was right before last period
            arr2 = prices[j + 1]
        else:                           # any other period (j is defined less than idx)
            arr2 = min(prices[(j + 1):idx])

        oc_ch = (prices[idx] - arr2) * self.efficiency - arr1
        oc_dis = min(prices[j:idx]) / self.efficiency

        return oc_ch, oc_dis

    def _calc_oc_before_first_charge(self, prices, t1_idx:int, idx:int):
        # opportunity cost before first charge
        oc_ch = min(prices[idx:t1_idx + 1]) * self.efficiency
        oc_dis =  min(prices[0:idx]) / self.efficiency

        return oc_ch, oc_dis

    def _calc_oc_after_last_discharge(self, prices, t_last, idx):
        # opportunity cost after last discharge
        if idx <= len(prices) - 2:
            oc_ch = max(prices[(idx + 1):]) * self.efficiency
        else:
            oc_ch = self.price_floor

        oc_dis = min(prices[t_last:idx+1]) / self.efficiency

        return oc_ch, oc_dis

    def _calc_oc_between_cycles(self, combined_list, prices, idx):

        # next discharge period
        j_next = idx + next((index for index, value in enumerate(combined_list[idx:]) if value > 0),None)
        # previous charge period
        j_prev = max((index for index, value in enumerate(combined_list[:idx]) if value < 0), default=None)

        if idx <= j_prev + 1:
            oc_ch = prices[j_prev]
        else:
            oc_ch = max(max(prices[(j_prev + 1):idx]) * self.efficiency, prices[j_prev])

        if idx >= j_next - 1:
            oc_dis = min(prices[j_next], prices[idx + 1] / self.efficiency)
        else:
            oc_dis = min(prices[j_next], min(prices[(idx + 1):j_next+1]) / self.efficiency)

        return oc_ch, oc_dis

    def _scheduler(self, prices):

        number_step =len(prices)
        # [START solver]
        # Create the linear solver with the GLOP backend.
        solver = pywraplp.Solver.CreateSolver("GLOP")
        if not solver:
            raise ImportError("Scheduler unable to load solver.")
        # [END solver]

        #Variables: all are continous
        charge = [solver.NumVar(0.0, self.chmax, "c"+str(i)) for i in range(number_step)]
        discharge = [solver.NumVar(0, self.dcmax,  "d"+str(i)) for i in range(number_step)]
        dasoc = [solver.NumVar(0.0, self.socmax, "b"+str(i)) for i in range(number_step+1)]
        dasoc[0]=0

        #Objective function
        solver.Minimize(
            sum(prices[i]*(charge[i]-discharge[i]) for i in range(number_step)))
        for i in range(number_step):
            solver.Add(dasoc[i] + self.efficiency*charge[i] - discharge[i] == dasoc[i+1])
        solver.Solve()
        #print("Solution:")
        #print("The Storage's profit =", solver.Objective().Value())
        charge_list = []
        discharge_list = []
        dasoc_list=[]
        for i in range(number_step):
            ch = charge[i].solution_value()
            dc = discharge[i].solution_value()
            self.logger.debug(f'quantities...\t time {i} \tcharge {ch} \tdischarge {dc}')
            charge_list.append(charge[i].solution_value())
            discharge_list.append(discharge[i].solution_value())
            #dasoc_list.append(dasoc[i].solution_value())

        assert sum(abs(c) for c in charge_list) > 0, "scheduler: charge list has no values"
        assert sum(abs(d) for d in discharge_list) > 0, "scheduler: discharge list has no values"

        return charge_list, discharge_list


if __name__ == '__main__':
    time_step = 28
    market_file = 'market_28.json'
    resource_file = 'resource_28.json'
    with open(market_file, 'r') as f:
        market_info = json.load(f)
    with open(resource_file, 'r') as f:
        resource_info = json.load(f)

    agent = Agent(time_step, market_info, resource_info)
    agent.logger.setLevel(logging.INFO)
    agent.make_me_an_offer()