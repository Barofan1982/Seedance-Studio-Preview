"""Separate analysis evidence from generation inputs and preserve edit requests."""
import re


_LOGO = r'logo|商标|品牌标志|品牌标识'
_REPLACE = r'替换|更换|换成|换为|换掉|改成|改为|replace|swap|change'
_REMOVE = r'去掉|去除|去水印|移除|删除|消除|抹掉|清除|清理|不要|不保留|remove|erase|delete|without|\bno\b'
_PRIORITY = '【优先修改要求】'
_BODY = '【画面与动作】'


def _clauses(text):
    # A filename can contain commas; keep the complete attachment handle.
    handles = []
    def protect(match):
        handles.append(match[0])
        return f'\x00{len(handles) - 1}\x00'
    protected = re.sub(r'@\{[^}]+\}', protect, str(text))
    return [re.sub(r'\x00(\d+)\x00', lambda m: handles[int(m[1])], part).strip()
            for part in re.split(r'[，,;；。\n]|但(?:是)?', protected) if part.strip()]


def _source_brief(brief):
    """Reuse user requirements, not our generic guard text, on another optimization."""
    text = str(brief).strip()
    if text.startswith(_PRIORITY) and '\n\n' + _BODY + '\n' in text:
        header, body = text.split('\n\n' + _BODY + '\n', 1)
        source = next((line.removeprefix('指定修改：') for line in header.splitlines()
                       if line.startswith('指定修改：')), '')
        return source + '\n' + body
    return text


def _without_guard(prompt):
    text = str(prompt).strip()
    if text.startswith(_PRIORITY) and '\n\n' + _BODY + '\n' in text:
        text = text.split('\n\n' + _BODY + '\n', 1)[1]
    # Legacy versions appended the block at the end.
    return re.sub(r'\n*【替换与清理要求】[\s\S]*$', '', text).strip()


def _logo_clause(part):
    words = re.sub(r'@\{[^}]+\}', '', part)
    if re.search(_LOGO, words, re.I):
        return True
    # "右下角替换为 @{logo.png}" is explicit enough; a filename alone
    # in a character/scene replacement sentence is not a logo edit.
    return bool(re.search(_LOGO, part, re.I) and re.search(_REPLACE, words, re.I)
                and not re.search(r'人物|角色|主角|场景|背景', words))


def edit_requirements(brief):
    text = _source_brief(brief)
    replace, remove = _REPLACE, _REMOVE
    targets = {'captions': r'字幕|subtitles?|captions?', 'watermarks': r'水印|watermarks?',
               'bottom_text': r'底部.{0,12}(?:文字|说明|解释|文案)|(?:bottom|lower).{0,20}(?:text|caption|description)'}
    clauses = _clauses(text)
    # Resolve explicit preservation before matching removal verbs; "不要删除"
    # and "不保留" must not be interpreted the same way.
    normalized = [re.sub(r'(?:不要|无需|不必|禁止|别|不)\s*(?:去掉|去除|移除|删除|消除|抹掉|清除|清理)', '保留', part)
                  for part in clauses]
    normalized = [re.sub(r'(?:不要|无需|不必|别|不)\s*保留', '去除', part) for part in normalized]
    normalized = [re.sub(r'(?:不要|无需|不必|禁止|别|不)\s*(?:替换|更换|修改|改变)', '保留', part)
                  for part in normalized]
    result = {'logo': any(re.search(replace, part, re.I) and _logo_clause(part) for part in clauses)}
    if re.search(r'(?:不要|无需|不必|禁止|别|不)(?:替换|更换|修改|改变)\s*(?:原有的?|原来的?|原)?\s*(?:logo|商标|品牌标志|品牌标识)', text, re.I):
        result['logo'] = False
    result['remove_logo'] = not result['logo'] and any(
        re.search(remove, part, re.I) and re.search(_LOGO, re.sub(r'@\{[^}]+\}', '', part), re.I)
        and not re.search(r'不(?:得|能)?(?:残留|保留)|不能.*原样复现', part)
        for part in normalized)
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


def _edit_clauses(brief):
    # Preserve user-supplied brand names, position, scope and exceptions verbatim.
    # The generic legacy guard is not a source of new user instructions.
    source = _source_brief(brief)
    body = _without_guard(source)
    candidates = _clauses(body)
    def relevant(part):
        return (re.search(_LOGO + r'|品牌|标识|字幕|水印|底部.{0,12}(?:文字|说明|解释|文案)', part, re.I)
                and re.search(_REPLACE + '|' + _REMOVE + r'|保留|keep|retain', part, re.I)
                and not part.startswith(('上述指定', '未指定修改')))
    selected = [part for part in candidates if relevant(part)]
    # In old drafts the AI sometimes omitted edits entirely. In that case the
    # appended guard is the only surviving copy of those requirements.
    if not selected and '【替换与清理要求】' in source:
        selected = [part for part in _clauses(source.split('【替换与清理要求】', 1)[1]) if relevant(part)]
    return list(dict.fromkeys(selected))


def logo_reference(brief, assets):
    """Resolve the requested replacement without treating a character as a logo."""
    if not edit_requirements(brief)['logo']:
        return None
    images = [a for a in assets if a.selected and a.kind == 'image' and a.purpose != 'keyframe']
    logos = [a for a in images if a.purpose == 'logo' or re.search(_LOGO + '|标志', a.filename, re.I)]
    clauses = [part for part in _edit_clauses(brief) if re.search(_LOGO, part, re.I)
               and re.search(_REPLACE, part, re.I)]
    # The reference on the right of "replace ... with ..." is the new logo.
    handles = []
    for part in clauses:
        match = re.search(r'(?:用|以)\s*(@\{[^}]+\})\s*(?:替换|更换|换掉)', part, re.I)
        if not match:
            match = re.search(r'(?:替换为|更换为|换成|换为|改成|改为|(?:替换|更换|replace|swap).{0,60}?(?:为|成|\bwith\b))\s*(@\{[^}]+\})', part, re.I)
        if match:
            handles.append(match[1])
        else:
            handles.extend(re.findall(r'@\{[^}]+\}', part))
    handles = list(dict.fromkeys(handles))
    if handles:
        eligible = {a.handle: a for a in images}
        missing = [h for h in handles if h not in eligible]
        if missing:
            raise ValueError('Logo 替换引用的图片未勾选或不存在，请检查：' + '、'.join(missing))
        if len(handles) == 1:
            return eligible[handles[0]]
        raise ValueError('Logo 替换对应多张图片，请在提示词中明确本次使用哪个新 Logo。')
    if len(logos) == 1:
        return logos[0]
    if len(logos) > 1:
        raise ValueError('已选择多张 Logo，请在提示词中用 @ 指定本次替换使用的图片。')
    raise ValueError('替换 Logo 缺少对应图片，请添加并勾选新 Logo，在提示词中用 @ 指定；人物图不会自动当作 Logo。')


def edit_analysis_instructions(brief, assets):
    if not cleanup_reference(brief):
        return ''
    target = logo_reference(brief, assets)
    binding = f'\n已核对新 Logo 附件：{target.handle}。' if target else ''
    return (
        '\n【定向替换与清理】修改优先级高于沿用原片、保留 UI 和保留其他文字；'
        '先区分必须替换、必须删除、明确保留三类，不得用“其他文字不变”覆盖指定品牌清理。'
        '工具会将已核对的品牌替换与文字清理前置到最终提示词首段，'
        'seedance_prompt 编写角色、动作、镜头和保留的界面，并补充从画面确认的具体品牌拼写与定位，'
        '不重复照抄工具的通用清理条款。'
        '逐帧观察旧品牌的准确拼写、大小写变体、位置及淡色副本，尤其检查人物衣服、背景、'
        '四角与弹窗前后；能看清的品牌名称写进最终提示词，看不清时只描述位置和外观，不编造拼写。'
        '替换旧品牌包括其文字、底形、描边、阴影和同品牌半透明字样，禁止新旧叠加。'
        '区分固定屏幕 Logo 与衣服、招牌等场景内 Logo：前者固定屏幕坐标与大小，'
        '后者跟随载体透视和运动；没有用户要求或画面证据时不要假定右下角、固定位置或透明背景。'
        '水印可能漂浮、滚动、移动、半透明或淡入淡出，要求全时段跟踪清理覆盖区域；'
        '未观察到的轨迹与精确时间不虚构。只清除用户指定范围，明确保留的字幕、说明、其他品牌不删。'
        '品牌替换之外仍执行用户的角色、场景与动作修改，不把“保持原片人物”写成冲突约束。'
        '最终自查：旧品牌清理范围、新 Logo 的完整 @ 引用、位置及运动方式、'
        '保留文字的例外是否一致；不得漏掉明确要求，也不要重复堆叠同义禁令。'
        + binding
        + '\n【工具固定前置约束】\n' + enforce_edit_requirements('', brief, assets).split('\n\n' + _BODY + '\n', 1)[0]
    )


def _remove_conflicts(prompt, rules, brief):
    """Drop direct preservation commands that contradict an explicit edit."""
    targets = []
    if rules['logo'] or rules['remove_logo']:
        targets.append(r'(?:原|旧|原有|原来)(?:的)?\s*(?:Logo|商标|品牌(?:标识|标志|字样)?)')
    for key, target in (('captions', '字幕'), ('watermarks', '水印'), ('bottom_text', '底部文字')):
        if rules[key]:
            targets.append(target)
    if not targets:
        return prompt
    target = '|'.join(targets)
    parts = re.split(r'([，,；;。\n])', prompt)
    user_clauses = _clauses(_source_brief(brief))
    for index in range(0, len(parts), 2):
        part = parts[index]
        if part.strip() in user_clauses:
            continue
        # "不得保留旧 Logo" is a cleanup instruction, not a conflict.
        affirmative = re.sub(r'(?:不要|不得|不能|不应|不|禁止|无需|不必)\s*保留', '清除', part)
        affirmative = re.sub(r'保留(?:新|用户指定).*', '', affirmative)
        if re.search(r'(?:保留|沿用)(?:原样|全部|所有)?\s*(?:' + target + ')', affirmative, re.I):
            parts[index] = ''
            if index + 1 < len(parts):
                parts[index + 1] = ''
    return ''.join(parts).strip()


def enforce_edit_requirements(prompt, brief, assets):
    rules = edit_requirements(brief)
    if not any(rules.values()):
        return prompt
    target = logo_reference(brief, assets)
    prompt = _remove_conflicts(_without_guard(prompt), rules, brief)
    clauses = _edit_clauses(brief)
    # The source line keeps exact brand names and placement, even if AI omitted them.
    instructions = ['指定修改：' + '；'.join(clauses)] if clauses else []
    if rules['logo']:
        instructions.append(f'所有出现镜头及转场先清除指定旧品牌的文字、底形、描边、阴影及同品牌淡色/半透明字样，'
                            f'修复覆盖区域，再呈现 {target.handle}；保留新 Logo 的图案、拼写、颜色与比例，不叠加新旧品牌。')
        source = '；'.join(clauses)
        if re.search(r'固定|角落|右下角|左下角|右上角|左上角|屏幕(?:叠加|标识)', source) and not re.search(r'衣服|衣领|胸前|招牌|包装|透视|跟随', source):
            instructions.append('该界面 Logo 固定于用户指定的屏幕位置，大小稳定；在要求显示的时段内，包括压暗和弹层过渡，保持可见，不随人物运动。')
        else:
            instructions.append('位置与运动按指定载体：界面 Logo 固定屏幕位置，衣服或招牌上的 Logo 跟随载体；不擅自新增角落标识。')
    elif rules['remove_logo']:
        instructions.append('全片清除指定旧 Logo 的文字、底形、描边、阴影及同品牌淡色副本，修复覆盖区域，不添加新的 Logo。')
    removed = [title for key, title in (('captions','字幕'),('watermarks','原视频水印'),('bottom_text','底部文字解释与说明条')) if rules[key]]
    if removed:
        instructions.append('全片去除' + '、'.join(removed) + '，恢复其覆盖区域的连续场景纹理；不要用模糊、马赛克、黑条或新文字代替。')
    if rules['watermarks']:
        instructions.append('逐帧跟踪清除漂浮、滚动、移动、淡入淡出及半透明的水印文字，覆盖全时段和全部移动路径；'
                            '同步修复经过的人物、物体和背景，不能只清固定角落或首帧，不留拖影、残字或重复印记。')
    instructions.append('以上修改优先于沿用原片与保留 UI；“其他文字不变”不包含指定旧品牌和待删除文字。'
                        '仅保留未要求修改的活动说明、数值、按钮及明确保留的内容；其他角色、场景、动作修改仍按下文执行。')
    return _PRIORITY + '\n' + '\n'.join(instructions) + '\n\n' + _BODY + '\n' + prompt
