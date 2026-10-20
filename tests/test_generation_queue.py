"""Queue lifecycle checks with temporary profiles and simulated paid endpoints."""
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from tests import test_studio_workflow as workflow
from studio_controller import collect_payload
from seedance_client import SubmissionUncertain, SeedanceError, build_payload
from media_assets import MediaAsset


class GenerationQueueTests(unittest.TestCase):
    setUpClass = classmethod(workflow.StudioWorkflowTests.setUpClass.__func__)
    setUp = workflow.StudioWorkflowTests.setUp
    tearDown = workflow.StudioWorkflowTests.tearDown
    new_controller = workflow.StudioWorkflowTests.new_controller
    pump_until = workflow.StudioWorkflowTests.pump_until
    make_window = workflow.StudioWorkflowTests.make_window

    def idle(self):
        return not self.controller.busy and not self.controller.query_busy

    def test_fifo_three_snapshots_download_and_later_draft_remains_independent(self):
        window = self.make_window()
        client = Mock()
        finished = set()
        sent = []
        def create(payload):
            sent.append(json.loads(json.dumps(payload)))
            return f'queue-video-{len(sent)}'
        def query(task_id):
            return {'id':task_id,'status':'succeeded' if task_id in finished else 'running',
                    'content':{'video_url':f'https://example.com/{task_id}.mp4'} if task_id in finished else {}}
        def download(_url, path):
            Path(path).write_bytes(b'video')
        client.create_task.side_effect = create
        client.get_task.side_effect = query
        with patch.object(self.controller,'seedance_client',return_value=client), \
             patch('studio_controller.download_video',side_effect=download), \
             patch('studio_controller.video_metadata',return_value={'duration':4}):
            self.controller.state.update(prompt='第一条',brief='第一条',download_dir=str(self.directory/'one'))
            first_payload = collect_payload(self.controller.state, [])
            first = self.controller.enqueue_generation(first_payload)
            first_payload['content'][0]['text'] = '修改外部参数不影响快照'
            self.pump_until(lambda:self.controller.row() and self.controller.row()['status']=='running' and self.idle())
            self.controller._poll.stop()
            for text, resolution in [('第二条','480p'),('第三条','1080p')]:
                self.controller.state.update(prompt=text,brief=text,resolution=resolution,download_dir=str(self.directory/'later'))
                self.controller.enqueue_generation()
            self.assertEqual(client.create_task.call_count,1)
            self.assertEqual([r['status'] for r in self.controller.queue.rows()],['running','waiting','waiting'])
            self.controller.state.update(prompt='还在编辑的第四条',brief='还在编辑的第四条',duration='7')
            window.editor_pages.setCurrentIndex(0)
            for index in range(1,4):
                self.controller._poll.stop()
                finished.add(f'queue-video-{index}')
                self.controller.query()
                self.pump_until(lambda:self.controller.queue.rows()[index-1]['status']=='succeeded')
                if index<3:
                    self.pump_until(lambda:len(sent)==index+1 and self.controller.row()['status']=='running' and self.idle())
                    self.assertEqual(window.editor_pages.currentIndex(),0)
            self.assertEqual([p['content'][0]['text'] for p in sent],['第一条','第二条','第三条'])
            self.assertEqual([p['resolution'] for p in sent],['720p','480p','1080p'])
            self.assertEqual(self.controller.state['prompt'],'还在编辑的第四条')
            self.assertTrue(Path(self.controller.row('queue-video-1')['local_path']).is_relative_to(self.directory/'one'))
            self.assertTrue(all(Path(r['local_path']).is_file() for r in self.controller.history.rows()))
            self.assertEqual(len([r for r in self.controller.history.calls() if r['operation']=='create']),3)
            self.assertEqual(window.editor_tabs.button(4).text(),'待提交')

    def test_pause_reorder_remove_and_resume_only_waiting_items(self):
        self.controller.history.add('outside-active',collect_payload(self.controller.state,[]))
        self.controller.state['task_id']='outside-active'
        identities=[]
        for text in ('A','B','C'):
            self.controller.state['prompt']=text
            identities.append(self.controller.enqueue_generation())
        self.controller.pause_queue()
        self.controller.move_queue_item(identities[2],-1)
        self.controller.remove_queue_item(identities[0])
        waiting=[r['queue_id'] for r in self.controller.queue.rows() if r['status']=='waiting']
        self.assertEqual(waiting,[identities[2],identities[1]])
        self.controller.history.update({'id':'outside-active','status':'failed'})
        client=Mock();client.create_task.return_value='after-pause';client.get_task.return_value={'id':'after-pause','status':'running'}
        with patch.object(self.controller,'seedance_client',return_value=client):
            self.controller.dispatch_queue();client.create_task.assert_not_called()
            self.controller.start_queue();self.controller.dispatch_queue()
            self.pump_until(lambda:self.controller.row('after-pause') and self.idle())
        self.controller._poll.stop()
        self.assertEqual(client.create_task.call_args[0][0]['content'][0]['text'],'C')
        with self.assertRaises(ValueError):self.controller.remove_queue_item(identities[2])
        self.assertEqual(self.controller.queue.get(identities[0])['status'],'cancelled')

    def test_uncertain_post_is_archived_next_item_runs_and_restart_never_reposts(self):
        snapshot=self.controller.generation_snapshot()
        first=self.controller.queue.add(snapshot)
        second=self.controller.queue.add(snapshot)
        client=Mock();client.create_task.side_effect=[SubmissionUncertain('创建连接超时'), 'next-task']
        client.get_task.return_value={'id':'next-task','status':'running'}
        with patch.object(self.controller,'seedance_client',return_value=client):
            self.controller.start_queue();self.controller.dispatch_queue()
            self.pump_until(lambda:self.controller.queue.get(second)['status']=='running' and self.idle())
            self.controller.dispatch_queue()
        self.assertEqual(client.create_task.call_count,2)
        self.assertEqual([r['status'] for r in self.controller.queue.rows()],['unknown','running'])
        self.assertTrue(self.controller.queue_enabled)
        self.assertFalse(self.errors)
        self.controller.close()
        restarted=self.new_controller()
        try:
            with patch.object(restarted,'seedance_client',return_value=client):
                restarted.resume()
                self.pump_until(lambda:not restarted.query_busy)
                restarted.dispatch_queue()
            self.assertFalse(restarted.queue_enabled)
            self.assertEqual(restarted.queue.get(second)['status'],'running')
            self.assertEqual(client.create_task.call_count,2)
            self.assertEqual(restarted.queue.get(first)['status'],'unknown')
        finally:restarted.close()

    def test_receipt_recovery_queries_existing_task_without_replaying_post(self):
        snapshot=self.controller.generation_snapshot()
        first=self.controller.queue.add(snapshot);second=self.controller.queue.add(snapshot)
        self.controller.queue.claim(first)
        call=self.controller.history.begin_call('create',{'queue_id':first})
        self.controller.queue.update(first,call_id=call)
        self.controller.history.finish_call(call,response={'id':'saved-receipt'})
        self.controller.close()
        restarted=self.new_controller()
        try:
            client=Mock();client.get_task.return_value={'id':'saved-receipt','status':'running'}
            with patch.object(restarted,'seedance_client',return_value=client):
                restarted.resume()
                self.pump_until(lambda:not restarted.query_busy)
                restarted.dispatch_queue()
            client.create_task.assert_not_called()
            self.assertEqual(restarted.queue.get(first)['task_id'],'saved-receipt')
            self.assertEqual(restarted.state['task_id'],'saved-receipt')
            self.assertEqual(restarted.queue.get(second)['status'],'waiting')
            self.assertFalse(restarted.queue_enabled)
        finally:restarted.close()

    def test_payload_bytes_and_signed_url_are_encrypted_and_survive_restore(self):
        payload=build_payload(prompt='快照测试',duration=4,resolution='720p',ratio='16:9',
                              image_sources=['data:image/png;base64,VEVTVC1JTUFHRQ=='],
                              video_sources=['https://example.com/a.mp4?signature=only-dummy-test-signature'])
        identity=self.controller.queue.add(self.controller.generation_snapshot(payload))
        self.assertEqual(self.controller.queue.get(identity)['snapshot']['payload'],payload)
        raw=self.controller.queue.path.read_bytes()
        self.assertNotIn(b'only-dummy-test-signature',raw)
        self.assertNotIn(b'VEVTVC1JTUFHRQ==',raw)
        self.controller.close();restarted=self.new_controller()
        try:self.assertEqual(restarted.queue.get(identity)['snapshot']['payload'],payload)
        finally:restarted.close()

    def test_definite_failure_and_expired_video_are_skipped_without_removal(self):
        first=self.controller.queue.add(self.controller.generation_snapshot())
        second=self.controller.queue.add(self.controller.generation_snapshot())
        client=Mock();client.create_task.side_effect=[SeedanceError('HTTP 400 invalid duration'),'next-task']
        client.get_task.return_value={'id':'next-task','status':'running'}
        with patch.object(self.controller,'seedance_client',return_value=client):
            self.controller.start_queue();self.controller.dispatch_queue()
            self.pump_until(lambda:self.controller.queue.get(second)['status']=='running' and self.idle())
            self.controller.dispatch_queue()
        self.assertEqual(client.create_task.call_count,2)
        self.assertEqual(self.controller.queue.get(first)['status'],'failed')
        self.assertEqual(self.controller.queue.get(second)['status'],'running')
        self.assertFalse(self.errors)
        self.controller._poll.stop()
        self.controller.history.update({'id':'next-task','status':'failed'})
        self.controller.sync_queue()
        snapshot=self.controller.generation_snapshot()
        snapshot['assets']=[{'path':'','kind':'video','purpose':'other','handle':'@{old.mp4}',
                            'source_url':'https://example.com/old.mp4','selected':True,'upload_id':'upload-old',
                            'source_expires_at':(datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat()}]
        expired=self.controller.queue.add(snapshot)
        with patch.object(self.controller,'seedance_client',return_value=client):
            self.controller.start_queue();self.controller.dispatch_queue()
        self.assertEqual(client.create_task.call_count,2)
        self.assertEqual(self.controller.queue.get(expired)['status'],'failed')
        self.assertIn('过期',self.controller.queue.get(expired)['error'])

    def test_one_generate_click_ignores_old_uncertain_records_and_retains_history(self):
        window=self.make_window()
        identity=self.controller.queue.add(self.controller.generation_snapshot())
        self.controller.queue.update(identity,status='unknown',error='原提交未收到回执')
        self.controller.queue_enabled=False
        self.controller.changed.emit('queue')
        self.assertFalse(window.queue_panel.show_past.isChecked())
        self.assertIn('1 条未收到回执', window.queue_panel.show_past.text())
        client=Mock();client.create_task.return_value='new-task'
        client.get_task.return_value={'id':'new-task','status':'running'}
        with patch.object(self.controller,'seedance_client',return_value=client):
            window.generate()
            self.pump_until(lambda:self.controller.row('new-task') and self.idle())
        self.assertEqual(window.editor_pages.currentIndex(),2)
        self.assertEqual(window.generate_button.text(),'生成视频 · 付费')
        self.assertEqual(self.controller.queue.get(identity)['status'],'unknown')
        self.assertFalse(self.errors)
        client.create_task.assert_called_once()
        window.edit_queued(identity)
        self.assertEqual(self.controller.queue.get(identity)['status'],'unknown')
        self.assertEqual(client.create_task.call_count,1)

    def test_unknown_can_associate_verified_task_without_new_generation(self):
        identity=self.controller.queue.add(self.controller.generation_snapshot())
        self.controller.queue.update(identity,status='unknown',error='未收到回执')
        client=Mock();client.get_task.return_value={'id':'verified-task','status':'running'}
        with patch.object(self.controller,'seedance_client',return_value=client):
            self.controller.attach_queue_task(identity,'verified-task')
            self.pump_until(lambda:self.controller.queue.get(identity)['status']=='running' and self.idle())
        self.controller._poll.stop()
        client.create_task.assert_not_called()
        self.assertEqual(self.controller.queue.get(identity)['task_id'],'verified-task')
        self.assertFalse(self.controller.queue_enabled)

    def test_download_failure_stops_queue_but_keeps_generated_video_in_history(self):
        first=self.controller.queue.add(self.controller.generation_snapshot())
        second=self.controller.queue.add(self.controller.generation_snapshot())
        client=Mock();client.create_task.return_value='download-failure'
        client.get_task.return_value={'id':'download-failure','status':'succeeded','content':{'video_url':'https://example.com/v.mp4'}}
        with patch.object(self.controller,'seedance_client',return_value=client), \
             patch('studio_controller.download_video',side_effect=OSError('本地磁盘无法写入')):
            self.controller.start_queue();self.controller.dispatch_queue()
            self.pump_until(lambda:self.controller.queue.get(first)['status']=='failed' and self.idle())
            self.controller.dispatch_queue()
        client.create_task.assert_called_once()
        self.assertEqual(self.controller.row()['status'],'succeeded')
        self.assertEqual(self.controller.queue.get(second)['status'],'waiting')
        self.assertFalse(self.controller.queue_enabled)

    def test_queue_panel_load_edit_preserves_current_task_and_removes_original(self):
        window=self.make_window()
        self.controller.history.add('still-running',collect_payload(self.controller.state,[]))
        self.controller.state['task_id']='still-running'
        self.controller.state['prompt']='排队的提示词'
        identity=self.controller.enqueue_generation()
        self.controller.pause_queue()
        window.editor_pages.setCurrentIndex(4);self.application.processEvents()
        self.assertTrue(window.queue_panel.isVisible())
        self.assertFalse(window.ai_button.isVisible())
        window.edit_queued(identity)
        self.assertEqual(window.editor_pages.currentIndex(),0)
        self.assertEqual(window.prompt.toPlainText(),'排队的提示词')
        self.assertEqual(self.controller.state['task_id'],'still-running')
        self.assertEqual(self.controller.queue.get(identity)['status'],'cancelled')


if __name__ == '__main__':
    unittest.main()
