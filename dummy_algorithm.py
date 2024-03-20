# This is a test dummy algorithm to get the opportunity cost curves
from ortools.linear_solver import pywraplp
import pandas as pd
import numpy as np
import argparse
import json
import datetime
from itertools import accumulate

# Standard battery parameters
socmax = 608
socmin = 128
chmax = 125
dcmax = 125
efficiency = 0.892

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

    def __init__(self, time_step, market_info, resource_info):
        # Data input from WEASLE
        self.step = time_step
        self.market = market_info
        self.resource = resource_info
        self.rid = resource_info['rid']

        # Standard battery parameters
        self.socmax = socmax
        self.socmin = socmin
        self.chmax = chmax
        self.dcmax = dcmax
        self.efficiency = efficiency

        # Configurable options
        self.price_ceiling = 999
        self.price_floor = 0

        self._prev_dam_file = 'prev_day_ahead_market'
        self.save_from_previous()

    def make_me_an_offer(self):
        # Read in information from the market
        market_type = self.market["market_type"]
        if market_type == 'DAM':
            offer = self._day_ahead_offer()
        elif market_type == 'RTM':
            offer = self._real_time_offer()

        #TODO: check if we need to clean up offers into maximum of 10 bins

        # Then save the result
        self._save_json(offer, f'offer_{self.step}')

    def save_from_previous(self):
        # if the current market type is DAM, then we need to save it in order to run RTM
        if 'DAM' in self.market["market_type"]:
            self._save_json(self.market['previous'], self._prev_dam_file)

    def _day_ahead_offer(self):
        # Make the offer curves and unload into arrays
        # type = self.market['market_type']
        type = self.market['uid'][:5]
        prices = self.market["previous"][type]["prices"]["EN"]
        self._calculate_offer_curve(prices)
        self._format_offer_curve()

        return self.formatted_offer

    def _format_offer_curve(self, required_times):
        # Offer parsing script below:
        required_times = [t for t in self.market['timestamps']]

        # Convert the offer curves to timestamp:offer_value dictionaries
        block_ch_mc = {}
        for i, cost in enumerate(self.charge_mc):
            block_ch_mc[required_times[i]] = float(cost)
        block_ch_mq = {}
        for i, power in enumerate(self.charge_mq):
            block_ch_mq[required_times[i]] = float(power)  # 125MW

        block_dc_mc = {}
        block_soc_mc = {}
        for i, cost in enumerate(self.discharge_mc):
            block_dc_mc[required_times[i]] = float(cost)
            block_soc_mc[required_times[i]] = 0

        block_dc_mq = {}
        block_soc_mq = {}
        for i, power in enumerate(self.discharge_mq):
            block_dc_mq[required_times[i]] = float(power)  # 125MW
            block_soc_mq[required_times[i]] = 0

        # estimate initial SoC for tomorrow's DAM
        t_init = datetime.datetime.strptime(self.market['timestamps'][0],'%Y%m%d%H%M')
        #t_now = self.market['current_time']
        t_now = t_init - datetime.timedelta(hours=15) #TODO: switch back once above in included in market_data
        t_init = t_init.strftime('%Y%m%d%H%M')
        t_now = t_now.strftime('%Y%m%d%H%M')
        schedule = self.resource['schedule'][self.rid]['EN']
        schedule_to_tomorrow = [q for t,q in schedule if t_now <= t < t_init]
        schedule_to_tomorrow = self._process_efficiency(schedule_to_tomorrow)
        soc_estimate = self.resource['status'][self.rid]['soc'] - sum(schedule_to_tomorrow)
        soc_estimate = min(self.socmax, max(soc_estimate, self.socmin))
        dispatch_estimate = self.resource['schedule'][self.rid]['EN'][t_init]

        # Package the dictionaries into an output formatted dictionary
        offer_out_dict = {self.rid: {}}
        offer_out_dict[self.rid] = {"block_ch_mc": block_ch_mc, "block_ch_mq": block_ch_mq, "block_dc_mc": block_dc_mc,
                               "block_dc_mq": block_dc_mq, "block_soc_mc": block_soc_mc, "block_soc_mq": block_soc_mq}
        offer_out_dict[self.rid].update(self._default_reserve_offer())
        offer_out_dict[self.rid].update(self._default_dispatch_capacity())
        offer_out_dict[self.rid].update(self._default_offer_constants(soc_begin=soc_estimate, init_en=dispatch_estimate))

        self.formatted_offer = offer_out_dict

    def _process_efficiency(self, data:list):
        processed_data = []
        for num in data:
            if num < 0:
                processed_data.append(num * self.efficiency)
            else:
                processed_data.append(num)
        return processed_data

    def _real_time_offer(self):
        initial_soc = resource_info["status"][self.rid]["soc"]
        soc_available = initial_soc - self.socmin
        soc_headroom = self.socmax - initial_soc
        block_dc_mc = {}
        block_dc_mq = {}
        block_ch_mc = {}
        block_ch_mq = {}
        block_soc_mc = {}
        block_soc_mq = {}

        t_end = self.market['timestamps'][-1]
        for t_now in self.market['timestamps']:
            en_ledger = {t:order for t,order in resource_info['ledger'][self.rid]['EN'] if t >= t_now}
            block_ch_mq[t_now] = []
            block_ch_mc[t_now] = []
            block_dc_mq[t_now] = []
            block_dc_mc[t_now] = []

            # add blocks for cost of current dispatch:
            for mq, mc in resource_info['ledger'][self.rid]['EN'][t_now]:
                if mq < 0:
                    soc_available += mq * self.efficiency
                    soc_headroom -= mq * self.efficiency
                    block_ch_mq[t_now].append(-mq)
                    block_ch_mc[t_now].append(mc)
                elif mq > 0:
                    soc_available -= mq
                    soc_headroom += mq
                    block_dc_mq[t_now].append(mq)
                    block_dc_mc[t_now].append(mc)

            # add blocks for soc available/headroom
            ledger_list = [tup for sublist in en_ledger.values() for tup in sublist]
            ledger_decreasing = sorted(ledger_list, key=lambda tup:tup[1], reverse=True)
            ledger_increasing = sorted(ledger_list, key=lambda tup:tup[1], reverse=False)
            # use decreasing price ledger for charging cost curve
            remaining_capacity = soc_headroom
            for mq, mc in ledger_decreasing:
                if 0 > mq >= -remaining_capacity:
                    remaining_capacity += mq * self.efficiency # remaining_capacity decreases because m<0
                    block_ch_mq[t_now].append(-mq)
                    block_ch_mc[t_now].append(mc)
                else:
                    remaining_capacity -= remaining_capacity
                    block_ch_mq[t_now].append(remaining_capacity)
                    block_ch_mc[t_now].append(mc)
                    break
            if remaining_capacity:
                block_ch_mq[t_now].append(remaining_capacity)
                block_ch_mc[t_now].append(self.price_floor)
            # use increasing price ledger for discharging cost curve
            remaining_capacity = soc_available
            for mq, mc in ledger_increasing:
                if 0 < mq <= remaining_capacity:
                    remaining_capacity -= mq
                    block_dc_mq[t_now].append(mq)
                    block_dc_mc[t_now].append(mc)
                else:
                    remaining_capacity -= remaining_capacity
                    block_dc_mq[t_now].append(remaining_capacity)
                    block_dc_mc[t_now].append(mc)
                    break
            if remaining_capacity:
                block_dc_mq[t_now].append(remaining_capacity)
                block_dc_mc[t_now].append(self.price_ceiling)

        # valuation of post-horizon SoC
        post_market_ledger = {t: order for t, order in resource_info['ledger'][self.rid]['EN'] if t > t_end}
        post_market_list = [tup for sublist in post_market_ledger.values() for tup in sublist]
        post_market_sorted = sorted(post_market_list, key=lambda tup:tup[1], reverse=True)
        block_soc_mq[t_end] = []
        block_soc_mc[t_end] = []
        remaining_capacity = soc_available
        for mq, mc in post_market_sorted:
            if 0 < mq <= remaining_capacity:
                remaining_capacity -= mq
                block_soc_mq[t_end].append(mq)
                block_soc_mc[t_end].append(mc)
            else:
                remaining_capacity -= remaining_capacity
                block_soc_mq[t_end].append(remaining_capacity)
                block_soc_mc.append(mc)
        if remaining_capacity:
            block_soc_mq[t_end].append(remaining_capacity)
            block_soc_mc[t_end].append(self.price_ceiling)
        block_soc_mq[t_end].append(soc_headroom)
        block_soc_mc[t_end].append(self.price_floor)


        # Package the dictionaries into an output formatted dictionary
        offer_out_dict = {self.rid: {}}
        offer_out_dict[self.rid] = {"block_ch_mc": block_ch_mc, "block_ch_mq": block_ch_mq, "block_dc_mc": block_dc_mc,
                               "block_dc_mq": block_dc_mq, "block_soc_mc": block_soc_mc, "block_soc_mq": block_soc_mq}
        offer_out_dict[self.rid].update(self._default_reserve_offer())
        offer_out_dict[self.rid].update(self._default_dispatch_capacity())
        offer_out_dict[self.rid].update(self._default_offer_constants(bid_soc=True))

        return offer_out_dict

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
        if hour_beginning in self.market['previous'][type]['timestamp']:
            prices = self.market['previous'][type]['EN']
            times = self.market['previous'][type]['timestamp']
        else:
            with open(self._prev_dam_file, "r") as file:
                prices = json.load(file)
                times = [key for key in prices.keys()]
                prices = [value for value in prices.values()]
        return prices, times

    def _save_json(self, save_dict, filename):
        # Save as json file in the current directory with name offer_{time_step}.json
        with open(f'offer_{self.step}.json', 'w') as f:
            json.dump(save_dict, f, indent=4, cls=NpEncoder)

    def _calculate_opportunity_costs(self, prices):

        self._scheduler(prices)

        # combine the charge/discharge list
        combined_list = [dis - ch for ch, dis in zip(self._charge_list, self._discharge_list)]

        # finding the index for first charge and last discharge
        t1_ch = next((index for index, value in enumerate(combined_list) if value < 0), None)
        t_last_dis = next((index for index in range(len(combined_list) - 1, -1, -1) if combined_list[index] > 0), None)

        # create two list for charging/discharging opportunity costs
        self._oc_dis_list = []
        self._oc_ch_list = []

        opportunity_costs = pd.DataFrame(None, index=range(len(prices)), columns=['Time', 'charge cost', 'disch cost'])
        soc = pd.DataFrame(None, index=range(len(prices) + 1), columns=['Time', 'SOC'])


        for index, row in opportunity_costs.iterrows():
            i = index
            row['Time'] = index

            # charging
            if combined_list[i] < 0:
                oc_ch, oc_dis = self._calc_oc_charge(combined_list, prices, i)
            # discharging
            elif combined_list[i] > 0:
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
            self._oc_ch_list.append(oc_ch)
            self._oc_dis_list.append(oc_dis)
            # save to dataframe
            row['charge cost'] = oc_ch
            row['disch cost'] = oc_dis

        return opportunity_costs

    def _calculate_offer_curve(self, prices):

        # marginal cost comes from opportunity cost calculation
        oc = self._calculate_opportunity_costs(prices)
        self.charge_mc = oc['charge cost'].values
        self.discharge_mc = oc['disch cost'].values

        # marginal quantities from scheduler values
        self.charge_mq = self._charge_list
        self.discharge_mq = self._discharge_list

    def _calc_oc_charge(self, combined_list, prices, idx):
        # opportunity cost during scheduled charge
        j = idx + 1 + next((index for index, value in enumerate(combined_list[idx + 1:]) if value > 0), None)
        oc_ch = min(prices[1:j], self.efficiency * prices[j]) if idx == 0 else min(np.delete(prices[0:j], idx).min(),
                                                                                 self.efficiency * prices[j])

        arr1 = prices[0] if idx == 0 else prices[0:idx].min()
        arr2 = 0 if j == idx + 1 else prices[idx + 1] if j == idx + 2 else prices[(idx + 1):j].min()
        oc_dis = oc_ch + 0.01 if idx == 0 else (-prices[idx] + arr1 + arr2) / self.efficiency

        return oc_ch, oc_dis

    def _calc_oc_discharge(self, combined_list, prices, idx):
        # opportunity cost during scheduled discharge
        j = max((index for index, value in enumerate(combined_list[:idx]) if value < 0), default=None)
        arr1 = 0 if idx == len(prices) else prices[idx + 1] if idx == len(prices) - 1 else prices[(idx + 1):].max()
        arr2 = 0 if j == idx - 1 else prices[j + 1] if j == idx - 2 else prices[(j + 1):idx].max()
        oc_ch = (-prices[idx] + arr1 + arr2) * self.efficiency
        oc_dis = max(prices[j] / self.efficiency, prices[(j + 1):].max())

        return oc_ch, oc_dis

    def _calc_oc_before_first_charge(self, prices, t1_idx, idx):
        # opportunity cost before first charge
        max_ch = 0 if idx == t1_idx - 1 else prices[idx + 1] if idx == t1_idx - 2 else prices[(idx + 1):t1_idx].max()
        oc_ch = max(max_ch * self.efficiency, prices[t1_idx])
        oc_dis = oc_ch + 0.01 if idx == 0 else prices[0] / self.efficiency if idx == 1 else prices[0:idx].min() / self.efficiency

        return oc_ch, oc_dis

    def _calc_oc_after_last_discharge(self, prices, t_last, idx):
        # opportunity cost after last discharge
        oc_ch = prices[(idx + 1):].max() * self.efficiency if idx < len(prices) - 2 else prices[idx + 1] if idx == len(
            prices) - 2 else np.min(prices)
        arr = prices[idx - 1] if idx == t_last + 2 else prices[(t_last + 1):idx] if idx > t_last + 2 else np.max(prices)
        oc_dis = min(prices[t_last], arr.min() / self.efficiency)

        return oc_ch, oc_dis

    def _calc_oc_between_cycles(self, combined_list, prices, idx):
        j_next = idx + 1 + next((index for index, value in enumerate(combined_list[idx + 1:]) if value > 0),None)
        j_prev = max((index for index, value in enumerate(combined_list[:idx]) if value < 0), default=None)
        oc_ch = 0 if idx < j_prev + 2 else prices[idx - 1] if idx == j_prev + 2 else max(
            prices[(j_prev + 1):idx].max() * self.efficiency, prices[j_prev])
        oc_dis = 0 if idx > j_next - 2 else min(prices[j_next],
                                              prices[idx + 1] / self.efficiency) if idx == j_next - 2 else min(
            prices[j_next], prices[(idx + 1):j_next].min() / self.efficiency)

        return oc_ch, oc_dis

    def _scheduler(self, prices):

        number_step =len(prices)
        # [START solver]
        # Create the linear solver with the GLOP backend.
        solver = pywraplp.Solver.CreateSolver("GLOP")
        if not solver:
            return
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
        self._charge_list=[]
        self._discharge_list=[]
        dasoc_list=[]
        for i in range(number_step):
            self._charge_list.append(charge[i].solution_value())
            self._discharge_list.append(discharge[i].solution_value())
            #dasoc_list.append(dasoc[i].solution_value())


if __name__ == '__main__':
    # Add argument parser for three required input arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('time_step', type=int, help='Integer time step tracking the progress of the\
                        simulated market.')
    parser.add_argument('market_info', help='json formatted dictionary with market information.')
    parser.add_argument('resource_info', help='json formatted dictionary with resource information.')

    args = parser.parse_args()

    # Parse json inputs into python dictionaries
    time_step = args.time_step
    market_info = json.loads(args.market_info)
    resource_info = json.loads(args.resource_info)