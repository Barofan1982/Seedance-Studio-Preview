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
        '\n【定向替换与清理】针对已有视频，优先写简短的修改说明：操作对象、从 A 改成 B、'
        '需要删除的内容、保持不变的范围。简单角色/Logo 替换通常 80–180 字，'
        '用户有复杂时序、数量、分镜要求时按需展开，不为字数删掉明确要求。'
        '原视频已经提供动作和镜头，不再逐项复述原片剧情，也不另外编排未要求修改的动作。'
        '同一条替换或清理要求只写一次，不输出“最高优先级”“指定修改”等重复模板。'
        '不把内部检查步骤写成生成指令，不添加逐帧修补、底形描边阴影逐项去除、'
        '转场覆盖等用户没有提出的操作流程。用户只要求替换 Logo 时，一句 A 替换为对应图片即可。'
        '实际查看人物衣服、背景与角落中的品牌：看得清的名称可用于准确定位，'
        '看不清时不编造拼写。用户明确要求清除同品牌淡字或滚动水印时简短写出该范围。'
        '用户给出位置/载体时照实保留，不自动假设右下角、固定坐标、透视运动或透明背景。'
        '保留要求不包含用户明确指定替换或删除的对象；明确要保留的字幕、说明及其他品牌不删除。'
        '角色和 Logo 的图片分别绑定，不遗漏用户的角色对应关系。'
        '最终自查附件引用、旧品牌名称、删除范围及保留例外；正确完整的短提示词直接返回，'
        '工具只补缺失要求，不再追加整段通用清理条款。'
        + binding
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


def _compact(text):
    return re.sub(r'[\s，,。；;：:、/]', '', text).casefold()


def _covered_edit(clause, prompt, target):
    """Recognize a few safe paraphrases; otherwise retain the user's exact clause."""
    compact = _compact(prompt)
    if _compact(clause) in compact:
        return True
    # Bind checks to a sentence about the same target, rather than finding a
    # logo handle in one paragraph and a character replacement in another.
    matching = [part for part in _clauses(prompt) if target and target.handle in part
                and re.search(_REPLACE, part, re.I)]
    if not target or target.handle not in clause or not matching:
        return False
    # If only the spelling of "replace" changed, all remaining source words
    # must still be represented. Keep unknown details instead of guessing intent.
    for part in matching:
        source = re.sub(_REPLACE + r'|统一|使用|原来的?|旧的?|Logo|logo|标识|为|成|将|把|的', '', clause)
        actual = re.sub(_REPLACE + r'|统一|使用|原来的?|旧的?|Logo|logo|标识|为|成|将|把|的', '', part)
        if _compact(source) and _compact(source) in _compact(actual):
            return True
    return False


def enforce_edit_requirements(prompt, brief, assets):
    rules = edit_requirements(brief)
    if not any(rules.values()):
        return prompt
    target = logo_reference(brief, assets)
    prompt = _remove_conflicts(_without_guard(prompt), rules, brief)
    prompt = re.sub(r'其他界面文字', '未指定修改的界面文字', prompt)
    prompt = re.sub(r'(?:所有|全部|其他)(文字|标识)(保持不变|不变)', r'未指定修改的\1\2', prompt)
    # Supplement concrete user requirements only. Do not turn the validation
    # checklist into hundreds of words of generic instructions for Seedance.
    missing = []
    for clause in _edit_clauses(brief):
        if not _covered_edit(clause, '\n'.join([prompt, *missing]), target):
            missing.append(clause)
    if target and not any(target.handle in part and re.search(_REPLACE, part, re.I)
                          for part in _clauses('\n'.join([prompt, *missing]))):
        missing.append(f'将原 Logo 替换为 {target.handle}')
    return ('；'.join(missing) + '。\n\n' + prompt).strip() if missing else prompt


def optimization_brief(brief, assets):
    """Do not feed our retired boilerplate back to AI as a new user requirement."""
    if _PRIORITY in brief or '【替换与清理要求】' in brief:
        return enforce_edit_requirements(_without_guard(brief), brief, assets)
    return brief
