"""Separate analysis evidence from generation inputs and preserve edit requests."""
import re


def edit_requirements(brief):
    text = str(brief)
    replace = r'替换|更换|换成|换为|换掉|改成|改为|replace|swap|change'
    remove = r'去掉|去除|去水印|移除|删除|消除|抹掉|清除|清理|不要|不保留|remove|erase|delete|without|\bno\b'
    targets = {'captions': r'字幕|subtitles?|captions?', 'watermarks': r'水印|watermarks?',
               'bottom_text': r'底部.{0,12}(?:文字|说明|解释|文案)|(?:bottom|lower).{0,20}(?:text|caption|description)'}
    clauses = re.split(r'[，,;；。\n]', text)
    # Resolve explicit preservation before matching removal verbs; "不要删除"
    # and "不保留" must not be interpreted the same way.
    normalized = [re.sub(r'(?:不要|无需|不必|禁止|别|不)\s*(?:去掉|去除|移除|删除|消除|抹掉|清除|清理)', '保留', part)
                  for part in clauses]
    normalized = [re.sub(r'(?:不要|无需|不必|别|不)\s*保留', '去除', part) for part in normalized]
    result = {'logo': bool(re.search(r'logo|商标|品牌标志|品牌标识', text, re.I)
                           and any(re.search(replace, part, re.I) and re.search(r'logo|商标|品牌标志|品牌标识', part, re.I)
                                   for part in clauses))}
    if re.search(r'(?:不要|无需|不必|禁止|别|不)(?:替换|更换|修改|改变)\s*(?:原有的?|原来的?|原)?\s*(?:logo|商标|品牌标志|品牌标识)', text, re.I):
        result['logo'] = False
    for name, target in targets.items():
        result[name] = any(re.search(remove, part, re.I) and re.search(target, part, re.I) for part in normalized)
        if any(re.search(r'(?:保留|keep|retain)(?:(?!去掉|去除|删除|移除|remove|erase).){0,8}(?:' + target + ')', part, re.I)
               for part in normalized):
            result[name] = False
    return result


def cleanup_reference(brief):
    return any(edit_requirements(brief).values())


def frames_for_generation(state):
    text = state.get('prompt', '').strip() or state.get('brief', '')
    return state.get('use_keyframes_for_generation') is True and not cleanup_reference(text)


def strip_analysis_references(text, catalog, assets, binding=''):
    text = str(text).replace(binding, '') if binding else str(text)
    # Remove the old automatically appended section when opening legacy drafts.
    text = re.sub(r'\n*【关键帧参考】\n[^\n]*仅用于构图、动作与镜头顺序；替换角色的外观以角色图为准。', '', text)
    videos = {a.handle for a in assets if a.kind == 'video' and a.selected and a.purpose != 'competitor'}
    mapping = {frame.get('handle'): frame.get('source_handle') for frame in catalog}
    mapping.update({a.handle: mapping.get(a.handle) for a in assets if a.purpose == 'keyframe'})
    for handle, source in mapping.items():
        if handle:
            text = text.replace(handle, source if source in videos else '')
    return text.strip()


def enforce_edit_requirements(prompt, brief, assets):
    rules = edit_requirements(brief)
    if not any(rules.values()):
        return prompt
    # This block is deterministic so AI omissions cannot erase explicit requests.
    prompt = re.sub(r'\n*【替换与清理要求】[\s\S]*$', '', prompt).rstrip()
    instructions = ['保持参考视频的镜头顺序、动作、节奏和构图，仅修改用户指定内容，不另编剧情。']
    images = [a for a in assets if a.selected and a.kind == 'image' and a.purpose != 'keyframe']
    logos = [a for a in images if a.purpose == 'logo' or re.search(r'logo|商标|标志', a.filename, re.I)]
    if not logos and len(images) == 1:
        logos = images
    if rules['logo']:
        target = logos[0].handle if len(logos) == 1 else '用户指定的 Logo 素材'
        instructions.append(f'将原 Logo 在所有出现镜头中替换为 {target}，保留新 Logo 的图案、拼写、颜色与比例；'
                            '对齐原位置、透视和运动，不得同时保留旧 Logo 或混合两个品牌。')
    removed = [title for key, title in (('captions','字幕'),('watermarks','原视频水印'),('bottom_text','底部文字解释与说明条')) if rules[key]]
    if removed:
        instructions.append('全片去除' + '、'.join(removed) + '，恢复其覆盖区域的连续场景纹理；不要用模糊、马赛克、黑条或新文字代替。')
    if rules['watermarks']:
        instructions.append('逐帧跟踪清除漂浮、滚动、移动、淡入淡出及半透明的水印文字，覆盖全时段和全部移动路径；'
                            '同步修复经过的人物、物体和背景，不能只清固定角落或首帧，不留拖影、残字或重复印记。')
    instructions.append('上述指定替换或删除的对象不能因参考原片而原样复现；未指定修改、或用户要求保留的标识与文字不变。')
    return prompt + '\n\n【替换与清理要求】\n' + '\n'.join(instructions)
