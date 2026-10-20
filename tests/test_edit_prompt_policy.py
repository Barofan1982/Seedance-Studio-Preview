"""Regression cases for branding that was lost despite a valid video reference."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_prompt import AgentPromptClient
from media_assets import MediaAsset
from openai_prompt import OpenAIPromptClient
from prompt_policy import edit_requirements, enforce_edit_requirements, frames_for_generation, logo_reference, optimization_brief


class EditPromptPolicyTests(unittest.TestCase):
    def setUp(self):
        self.video = MediaAsset(None, 'video', 'other', '@{替换.mp4}', 'https://example.invalid/reference.mp4', 12)
        self.character = MediaAsset(Path('参考图.png'), 'image', 'character', '@{参考图.png}')
        self.logo = MediaAsset(Path('logo.png'), 'image', 'other', '@{logo.png}')
        self.assets = [self.video, self.character, self.logo]
        self.brief = (
            '以 @{参考图.png} 左、右人物分别替换 @{替换.mp4} 中左、右人物。'
            '保留活动说明文字和绿色领取按钮，保持原有 UI 层级及其他界面文字。'
            '全片清除原 Food Stars/FoodStars 标识及淡色同品牌字样，'
            '右下角统一替换为 @{logo.png}，不残留旧品牌。')

    def test_omitted_brand_names_and_logo_binding_are_restored_ahead_of_body(self):
        prompt = enforce_edit_requirements(
            '保留原 Logo。两人欢笑，手指点击领取。保持原有 UI 层级及其他界面文字。', self.brief, self.assets)
        for value in ('Food Stars/FoodStars', self.logo.handle, '右下角', '淡色同品牌'):
            self.assertIn(value, prompt)
        self.assertIn('两人欢笑，手指点击领取', prompt)
        self.assertNotIn('保留原 Logo', prompt)
        self.assertIn('未指定修改的界面文字', prompt)
        self.assertLess(len(prompt), 200)
        for extra in ('【优先修改要求】', '逐帧', '底形、描边', '修复覆盖区域'):
            self.assertNotIn(extra, prompt)
        self.assertNotIn('保持原人物', prompt)
        self.assertFalse(frames_for_generation({'prompt': prompt, 'use_keyframes_for_generation': True}))

    def test_other_locations_and_carriers_do_not_get_fixed_bottom_right_template(self):
        for request in ('衣服胸前的旧 Logo 替换为 @{logo.png}，跟随衣服运动',
                        '招牌上的旧 Logo 替换为 @{logo.png}，保持透视'):
            with self.subTest(request=request):
                prompt = enforce_edit_requirements('人物走动', request, self.assets)
                self.assertIn('衣服' if '衣服' in request else '招牌', prompt)
                self.assertNotIn('不随人物运动', prompt)
                self.assertNotIn('右下角', prompt)

    def test_explicit_target_wins_over_filename_guess_and_unselected_target_fails(self):
        alternate = MediaAsset(Path('新图.png'), 'image', 'other', '@{新图.png}')
        self.assertEqual(logo_reference('原 Logo 替换为 @{新图.png}', [*self.assets, alternate]), alternate)
        self.assertEqual(logo_reference('用 @{新图.png} 替换 @{参考图.png} 上的 Logo', [*self.assets, alternate]), alternate)
        alternate.selected = False
        with self.assertRaisesRegex(ValueError, '未勾选或不存在'):
            logo_reference('原 Logo 替换为 @{新图.png}', [*self.assets, alternate])
        with self.assertRaisesRegex(ValueError, '缺少对应图片'):
            logo_reference('替换旧 Logo', [self.video, self.character])
        extra = MediaAsset(Path('logo2.png'), 'image', 'other', '@{logo2.png}')
        with self.assertRaisesRegex(ValueError, '多张 Logo'):
            logo_reference('替换旧 Logo', [*self.assets, extra])

    def test_remove_only_and_explicit_preservation_do_not_require_logo_image(self):
        prompt = enforce_edit_requirements('保持镜头', '去掉旧 Logo，保留字幕', [])
        self.assertIn('去掉旧 Logo', prompt)
        self.assertNotIn('@{', prompt)
        self.assertNotIn('全片去除字幕', prompt)
        rules = edit_requirements('不要替换 Logo，保留字幕，去掉滚动水印')
        self.assertFalse(rules['logo'])
        self.assertFalse(rules['remove_logo'])
        prompt = enforce_edit_requirements('不要保留水印。保留字幕。', '不要替换 Logo，保留字幕，去掉滚动水印', [])
        self.assertIn('保留字幕', prompt)
        self.assertIn('滚动', prompt)
        self.assertNotIn('逐帧跟踪', prompt)
        self.assertNotIn('不添加新的 Logo', prompt)
        self.assertFalse(edit_requirements('用 @{logo.png} 替换人物')['logo'])

    def test_explicit_exception_for_other_brand_is_preserved(self):
        brief = '原 Logo 替换为 @{logo.png}，保留原品牌的赞助商标识'
        prompt = enforce_edit_requirements('保留原品牌的赞助商标识。角色挥手。', brief, self.assets)
        self.assertIn('保留原品牌的赞助商标识', prompt)

    def test_reoptimization_and_legacy_guard_do_not_duplicate_or_eat_actions(self):
        legacy = '人物欢笑，手指点击领取。\n\n【替换与清理要求】\n保持参考视频。'
        once = enforce_edit_requirements(legacy, self.brief, self.assets)
        self.assertNotIn('【替换与清理要求】', once)
        self.assertEqual(enforce_edit_requirements(once, self.brief, self.assets), once)
        self.assertEqual(enforce_edit_requirements(once, once, self.assets), once)
        self.assertIn('人物欢笑，手指点击领取', once)
        self.assertLess(len(once), 650)
        legacy_only = '人物欢笑。\n\n【替换与清理要求】\n将原 Logo 在所有出现镜头中替换为 @{logo.png}。全片去除字幕。'
        migrated = enforce_edit_requirements(legacy_only, legacy_only, self.assets)
        self.assertIn('全片去除字幕', migrated)
        self.assertEqual(enforce_edit_requirements(migrated, migrated, self.assets), migrated)

    def test_complete_short_prompt_is_not_expanded(self):
        short = '把 @{替换.mp4} 左右人物换成 @{参考图.png} 对应人物。右下角原 Logo 替换为 @{logo.png}，去掉滚动水印，保留字幕。其余动作和界面不变。'
        self.assertEqual(enforce_edit_requirements(short, short, self.assets), short)
        brief = '右下角统一替换为 @{logo.png}'
        self.assertEqual(enforce_edit_requirements('右下角 Logo 换成 @{logo.png}', brief, self.assets), '右下角 Logo 换成 @{logo.png}')

    def test_retired_header_is_not_sent_to_analysis_and_real_requests_survive(self):
        old = ('【优先修改要求】\n指定修改：全片清除 FoodStars 标识；右下角统一替换为 @{logo.png}\n'
               '所有出现镜头及转场先清除底形描边阴影，再修复背景。\n\n【画面与动作】\n'
               '以 @{参考图.png} 替换 @{替换.mp4} 的人物，保留镜头。')
        cleaned = optimization_brief(old, self.assets)
        for value in ('FoodStars', '右下角', self.logo.handle, self.character.handle, self.video.handle):
            self.assertIn(value, cleaned)
        self.assertNotIn('描边阴影', cleaned)
        self.assertNotIn('【优先修改要求】', cleaned)
        self.assertLess(len(cleaned), 160)
        old_using = old.replace('以 @{参考图.png} 替换 @{替换.mp4} 的人物，保留镜头。',
                                '以 @{参考图.png} 替换人物，右下角统一使用 @{logo.png}。')
        cleaned_using = optimization_brief(old_using, self.assets)
        self.assertTrue(edit_requirements(cleaned_using)['logo'])
        self.assertEqual(logo_reference(cleaned_using, self.assets), self.logo)

    def test_both_ai_transports_receive_same_policy_and_return_guarded_prompt(self):
        data = {'analysis': '保留活动流程', 'shots': [], 'seedance_prompt': '两人欢笑，点击领取。',
                'uncertainties': [], 'keyframe_handles': ['@{frame.jpg}']}
        response = {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(data)}]}]}
        session = Mock()
        session.complete.return_value = data
        clients = [OpenAIPromptClient('synthetic', chatgpt_plan=True), AgentPromptClient(session, 'synthetic-model')]
        with patch('openai_prompt._scaled_jpeg_data_url', return_value='data:image/jpeg;base64,eA=='):
            for client in clients:
                with self.subTest(client=type(client).__name__), patch.object(client, '_stream_response', return_value=response) as stream:
                    draft = client.draft(None, self.assets, self.brief, reference_frames=[])
                    request = stream.call_args.args[0] if client.chatgpt_plan else session.complete.call_args.args[0]
                    content = request['input'][0]['content']
                    self.assertIn('已核对新 Logo 附件：' + self.logo.handle, content[0]['text'])
                    self.assertIn('人物衣服、背景', content[0]['text'])
                    self.assertNotIn('【工具固定前置约束】', content[0]['text'])
                    self.assertIn('通常 80–180 字', content[0]['text'])
                    self.assertIn('同一条替换或清理要求只写一次', content[0]['text'])
                    self.assertIn('不得漏掉旧品牌清理范围', request['instructions'])
                    self.assertTrue(all(c['detail'] == 'high' for c in content if c['type'] == 'input_image'))
                    self.assertNotIn('【优先修改要求】', draft.prompt)
                    self.assertIn('Food Stars/FoodStars', draft.prompt)
                    self.assertEqual(draft.keyframe_handles, [])

    def test_invalid_logo_reference_stops_before_paid_request_or_frame_extraction(self):
        with patch.object(OpenAIPromptClient, '_stream_response') as send, patch('openai_prompt.sample_video_frames') as frames:
            with self.assertRaisesRegex(ValueError, '未勾选或不存在'):
                OpenAIPromptClient('synthetic', chatgpt_plan=True).draft(
                    None, [self.character], '用 @{missing.png} 替换原 Logo')
            send.assert_not_called()
            frames.assert_not_called()


if __name__ == '__main__':
    unittest.main()
