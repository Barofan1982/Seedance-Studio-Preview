"""Logo cleanup must analyze video frames without submitting them to generation."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage
from media_assets import MediaAsset, image_data_url
from openai_prompt import OpenAIPromptClient, PromptDraft
from studio_controller import StudioController, collect_payload, manifest
from studio_widgets import KeyframeDialog
from prompt_policy import edit_requirements, enforce_edit_requirements


class AnalysisOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.directory = Path(self.temp.name)
        auth = Mock(); auth.active_account.return_value = {'email':'synthetic@example.invalid'}
        auth.label.return_value = '测试账号'; auth.access_token.return_value = 'synthetic-oauth'
        self.auth = auth
        with patch('studio_controller.load_key',return_value=''), patch('studio_controller.load_openai_key',return_value=''):
            self.controller = StudioController(self.directory,auth=auth)
        self.controller.models = [{'slug':'test-model'}]
        self.controller.state.update(openai_model='test-model',mode='全模态参考',watermark=True)
        self.video = MediaAsset(None,'video','other','@{demo.mp4}','https://example.com/demo.mp4',8)
        self.logo_path = self.directory/'logo.png'
        self.frame_path = self.directory/'frame.jpg'
        for path, color in ((self.logo_path,0xff00ff00),(self.frame_path,0xffff0000)):
            image=QImage(320,480,QImage.Format.Format_RGB32);image.fill(color);image.save(str(path))
        self.logo=MediaAsset(self.logo_path,'image','other','@{logo.png}')
        self.catalog=[{'path':str(self.frame_path),'handle':'@{demo-00.00s.jpg}','source_handle':self.video.handle,'timestamp':0}]
        self.frame=MediaAsset(self.frame_path,'image','keyframe',self.catalog[0]['handle'])
        self.brief='参考 @{demo.mp4}，替换logo为 @{logo.png}，去掉字幕、水印以及底部文字解释。'
        self.controller.assets=[self.video,self.logo]
        self.controller.state.update(prompt=self.brief,brief=self.brief)

    def tearDown(self):
        self.controller.close(); self.application.processEvents(); self.temp.cleanup()

    def test_optimization_preserves_cleanup_and_only_submits_video_plus_logo(self):
        # Simulate an AI ignoring the edit requests and recommending a source frame.
        bad=PromptDraft('分析了画面',[],'参考 @{demo-00.00s.jpg} 的风格',[],keyframe_handles=[self.frame.handle])
        with patch('studio_controller.frame_catalog',return_value=self.catalog), patch('studio_controller.OpenAIPromptClient') as factory:
            factory.return_value.draft.return_value=bad
            self.controller.analyze()
            deadline=time.monotonic()+4
            while self.controller.busy and time.monotonic()<deadline:
                self.application.processEvents();time.sleep(.01)
            self.assertFalse(self.controller.busy)
            self.assertEqual(factory.return_value.draft.call_args.kwargs['reference_frames'],self.catalog)
            self.assertFalse(factory.return_value.draft.call_args.kwargs['use_keyframes_for_generation'])
        payload=collect_payload(self.controller.state,self.controller.assets)
        self.assertEqual(sum(b['type']=='video_url' for b in payload['content']),1)
        images=[b for b in payload['content'] if b['type']=='image_url']
        self.assertEqual(len(images),1)
        self.assertEqual(images[0]['image_url']['url'],image_data_url(self.logo_path))
        self.assertNotIn(image_data_url(self.frame_path),json.dumps(payload))
        self.assertFalse(payload['watermark'])
        for term in ('字幕','水印','底部文字解释','logo.png'):
            self.assertIn(term,self.controller.state['prompt'])
        self.assertLess(len(self.controller.state['prompt']), 200)
        self.assertNotIn('【优先修改要求】', self.controller.state['prompt'])
        self.assertNotIn('逐帧跟踪', self.controller.state['prompt'])
        self.assertNotIn(self.frame.handle,self.controller.state['prompt'])
        self.assertFalse(any(a.purpose=='keyframe' for a in self.controller.assets))
        self.assertEqual(self.controller.state['frame_catalog'],self.catalog)
        call=self.controller.history.calls()[0]
        self.assertEqual(call['status'],'succeeded')
        self.assertFalse(json.loads(call['request_json'])['use_keyframes_for_generation'])
        dialog=KeyframeDialog(self.controller,Mock())
        self.assertFalse(dialog.attach_frames.isEnabled());self.assertFalse(dialog.attach_frames.isChecked())
        dialog.close();self.application.processEvents()

    def test_final_payload_guard_excludes_stale_frames_even_if_previously_enabled(self):
        self.controller.assets.append(self.frame)
        self.controller.state.update(frame_catalog=self.catalog,use_keyframes_for_generation=True)
        payload=collect_payload(self.controller.state,self.controller.assets)
        self.assertEqual(sum(b['type']=='image_url' for b in payload['content']),1)
        with self.assertRaisesRegex(ValueError,'仅供 AI 分析'):
            self.controller.apply_keyframes([self.frame.handle])

    def test_legacy_restart_and_history_reuse_remove_generated_references(self):
        binding='\n\n【关键帧参考】\n'+self.frame.handle+' 仅用于构图、动作与镜头顺序；替换角色的外观以角色图为准。'
        self.controller.assets.append(self.frame)
        self.controller.state.update(frame_catalog=self.catalog,prompt=self.brief+binding,generated_keyframe_binding=binding)
        self.controller.save()
        with patch('studio_controller.load_key',return_value=''), patch('studio_controller.load_openai_key',return_value=''):
            restarted=StudioController(self.directory,auth=self.auth)
        try:
            self.assertFalse(any(a.purpose=='keyframe' for a in restarted.assets))
            self.assertNotIn(self.frame.handle,restarted.state['prompt'])
            self.assertEqual(restarted.state['frame_catalog'],self.catalog)
            record={'settings':{'mode':'reference'},'assets':manifest([self.video,self.logo,self.frame]),
                    'original_prompt':self.brief+binding,'frames':self.catalog,'draft':{'analysis':'旧版核对','keyframe_handles':[self.frame.handle]}}
            restarted.reuse_prompt(record)
            self.assertEqual(sum(b['type']=='image_url' for b in collect_payload(restarted.state,restarted.assets)['content']),1)
            self.assertNotIn('【关键帧参考】',restarted.state['prompt'])
        finally: restarted.close()

    def test_ai_receives_frames_and_strict_edit_instructions_but_returns_no_frame_attachments(self):
        response={'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({
            'analysis':'画面核对','shots':[],'seedance_prompt':'保留动作','uncertainties':[],
            'keyframe_handles':[self.frame.handle]})}]}]}
        with patch.object(OpenAIPromptClient,'_stream_response',return_value=response) as send:
            draft=OpenAIPromptClient('synthetic-oauth',chatgpt_plan=True).draft(None,[self.video,self.logo],self.brief,reference_frames=self.catalog)
        content=send.call_args.args[0]['input'][0]['content']
        self.assertEqual(sum(b['type']=='input_image' for b in content),2)
        self.assertIn('【定向替换与清理】',content[0]['text'])
        self.assertIn('keyframe_handles 必须返回空数组',content[0]['text'])
        self.assertEqual(draft.keyframe_handles,[])
        self.assertIn('去掉字幕、水印以及底部文字解释',draft.prompt)

    def test_requested_preserved_text_is_not_deleted_and_policy_is_idempotent(self):
        rules=edit_requirements('保留字幕，去掉水印，替换logo为 @{logo.png}')
        self.assertFalse(rules['captions']);self.assertTrue(rules['watermarks']);self.assertTrue(rules['logo'])
        self.assertFalse(edit_requirements('不要替换logo，去水印')['logo'])
        self.assertTrue(edit_requirements('不要替换logo，去水印')['watermarks'])
        self.assertFalse(edit_requirements('不要删除字幕，去掉滚动水印')['captions'])
        self.assertTrue(edit_requirements('不保留半透明水印文字')['watermarks'])
        self.assertFalse(edit_requirements('不要去掉水印')['watermarks'])
        first=enforce_edit_requirements('保持动作',self.brief,[self.video,self.logo])
        self.assertEqual(enforce_edit_requirements(first,self.brief,[self.video,self.logo]),first)
