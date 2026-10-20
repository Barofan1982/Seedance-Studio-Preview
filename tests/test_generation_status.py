import unittest
from generation_status import generation_feedback, waiting_estimate


class GenerationStatusTests(unittest.TestCase):
    def test_stage_and_elapsed_follow_actual_server_status_and_freeze_when_complete(self):
        call={'status':'succeeded','created_at':100,'duration_ms':10000}
        row={'status':'running','created_at':110,'task_id':'task','local_available':False}
        running=generation_feedback(call,row,{'submitted_at':100},[],{},now=190)
        self.assertEqual(running['phase'],2)
        self.assertEqual(running['elapsed'],'已等待 01:30')
        self.assertTrue(running['active'])
        self.assertNotIn('%',running['estimate'])
        paused=generation_feedback(call,row,{'submitted_at':100},[],{},paused=True,now=200)
        self.assertFalse(paused['active'])
        self.assertIn('继续查询',paused['detail'])
        row.update(status='succeeded',local_available=True)
        for now in (1000,10000):
            complete=generation_feedback(call,row,{'submitted_at':100,'finished_at':250},[],{},now=now)
            self.assertEqual(complete['elapsed'],'耗时 02:30')
            self.assertEqual(complete['phase'],4)
            self.assertFalse(complete['active'])

    def test_submit_timeout_is_uncertain_and_does_not_display_an_old_video_as_the_result(self):
        call={'status':'unknown','created_at':100,'duration_ms':120000}
        row={'status':'succeeded','task_id':'older-video','local_available':True}
        value=generation_feedback(call,row,{},[],{},now=500)
        self.assertEqual(value['task_id'],'')
        self.assertEqual(value['title'],'提交未确认')
        self.assertIn('记录已保存，可继续生成',value['detail'])
        self.assertIn('可能额外计费',value['detail'])
        self.assertFalse(value['active'])
        self.assertEqual(value['elapsed'],'耗时 02:00')

    def test_early_reset_is_visible_with_actual_old_timeout_and_no_fake_wait(self):
        call={'status':'unknown','created_at':100,'duration_ms':61517,
              'response_json':{'network_diagnostics':{'reason_type':'ConnectionResetError','winerror':10054,
                                                    'timeout_seconds':300}}}
        value=generation_feedback(call,None,{},[],{},now=500)
        self.assertIn('62 秒',value['detail'])
        self.assertIn('10054',value['detail'])
        self.assertIn('300 秒',value['detail'])
        self.assertNotIn('超时',value['detail'])
        self.assertFalse(value['active'])
        self.assertEqual(value['elapsed'],'耗时 01:01')

    def test_estimates_use_matching_local_observations_and_do_not_count_down_below_zero(self):
        samples=[{'model':'m','resolution':'480p','requested_duration':-1,'seconds':value} for value in (200,250,300)]
        samples.append({'model':'other','resolution':'1080p','requested_duration':30,'seconds':1500})
        text,source=waiting_estimate(samples,{'model':'m','resolution':'480p','duration':-1},0)
        self.assertIn('3–6 分钟',text)
        self.assertIn('3 次同类',source)
        self.assertIn('超过近期预估',waiting_estimate(samples,{'model':'m','resolution':'480p','duration':-1},999)[0])
        self.assertIn('暂无历史样本',waiting_estimate([],{},0)[0])


if __name__=='__main__':unittest.main()
