import unittest
from decimal import Decimal

from billing_cny import DEFAULT_CNY_RATES, cny_summary, estimate_cny, parse_cny_rates


class CnyBillingTests(unittest.TestCase):
    def record(self,with_video,tokens=250000,status='succeeded'):
        return {'status':status,'model':'seedance-2.5-480p-720p-'+('with-video' if with_video else 'no-video'),
                'completion_tokens':tokens,'estimated_cost_usd':999}

    def test_yuan_rates_follow_actual_video_input_without_using_dollar_estimates(self):
        self.assertEqual(estimate_cny(self.record(False),DEFAULT_CNY_RATES),Decimal('17.5'))
        self.assertEqual(estimate_cny(self.record(True),DEFAULT_CNY_RATES),Decimal('10.5'))
        totals=cny_summary([self.record(False),self.record(True),self.record(True,status='failed')],DEFAULT_CNY_RATES)
        self.assertEqual(totals,{'amount':Decimal('28'),'priced':2,'unpriced':0})

    def test_missing_rates_or_usage_are_not_treated_as_zero_cost(self):
        self.assertIsNone(estimate_cny(self.record(True),{}))
        self.assertIsNone(estimate_cny(self.record(True,tokens=None),DEFAULT_CNY_RATES))
        self.assertIsNone(cny_summary([self.record(True)],{})['amount'])
        self.assertEqual(estimate_cny(self.record(True),{'with_video':0}),Decimal(0))
        for tokens in (-1,True,float('nan'),float('inf')):
            self.assertIsNone(estimate_cny(self.record(True,tokens=tokens),DEFAULT_CNY_RATES))
        for price in ('bad','-1','nan','inf'):
            with self.assertRaises(ValueError):parse_cny_rates({'with_video':price})
        self.assertEqual(parse_cny_rates({'without_video':'70','with_video':''}),{'without_video':70.0})


if __name__=='__main__':unittest.main()
