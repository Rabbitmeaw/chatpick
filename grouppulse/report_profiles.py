"""本机批准的服务器编辑背景；不保存真实人物资料、不替代原报告核心。"""

import re

PROFILE_ID = 'us-stock-chat'
SECTIONS = ('stock_opinion', 'news', 'chat')

LAYOUT_INSTRUCTIONS = '''
本次服务器专属约定，沿用原报告JSON和整理函数：
先逐条提取reportProfile.focusAuthors本期实质分析；不能因发言少或夹在新闻中就省略。成员任务仍仅写目标人物。
topics增加section、asset。section仅stock_opinion、news、chat；asset为单一主标的代码，未指明或非单一标的时null。一个主标的一条话题：同一人分别谈AAA、BBB，应返回两个stock_opinion，asset分别为AAA、BBB，不得合为“某人谈AAA与BBB”。同一标的才合并各人观点，分歧并列。比较对象不另造观点，纳指/标普不擅自替成ETF。
stock_opinion是个人分析：支撑/压力、站稳/跌破/开盘条件、价格区间、可能走势、估值和个人操作看法，也包括没有单一标的的仓位/投资纪律看法（asset为null）。SOXX、SOXL、QQQ各是不同标的，点位各列一条观点，不能夹入拆股资讯、政治或生活话题。数字仅取自本批原文。同条原话兼有走势分析与拆股日期，放在该标的观点里，日期附带说明，不重复到news。
news仅是谁分享/转述的消息或见闻；chat保留生活、旅行和有趣互动。不为维持四个热点合并不同标的或删闲聊，同一原话涉及不同主标的可分别引用同一真实ID。
全报告包括标题/hero都保留作者和条件，写“某人认为/自述/分享”，不写成已核实事实或群体决定。标题只写内容，不用“喊单/荐股/带单/交易建议”等标签；个人看法用间接引语，不变成助手的买卖指令、仓位安排或行动清单，不套免责声明。不改变原发言对象：对群友的提醒仍写成该作者对群友的提醒，不能补成“她自己的操作”或“不是对别人说”；引用他人仍注明转述。保留发言不等于摘要在向读者提供交易建议。
每批仅描述已提供片段中的实际发言；不能从本批未见外推“此人今天没有说”“仅他人转述、本人未表态”等全周期缺席结论。明天等相对日期以该条原文时间为准。跨批/跨日由应用按标的及真实时点排列，不能臆造转向、兑现或因果。每条话题只谈其单一主标的，比较对象不能自动获得独立观点。
理解含混、引用不足或语义推断把握低的topic增加confidence:"low"和reviewReason，简述具体疑点；原词和条件保留，不猜解、不删除，不虚构置信百分比。普通明确条目不必增加字段。这只是模型自评，不能声称已核实。
hero.summary用3至5个短要点点题，不重复展开。每条摘要自然具体、附对应sourceMessageIds；空板块不占位，conclusions/importantMessages不重复正文，无明确任务/问题则todos/unresolved为空。沿用原JSON，仅附加section、asset及可选低置信度字段。
'''


def primary_asset(topic):
    value = topic.get('asset')
    return value.upper() if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9.-]{0,15}', value) else None


def select_profile(app, server, kind):
    if kind not in ('daily', 'member'):
        return None
    account = getattr(app, 'account_scope', {}).get('account_id')
    if not account:
        origin = getattr(app, 'metadata', {}).get('origin', {})
        if origin.get('kind') == 'account-export':
            account = origin.get('account_id')
    if not account:
        return None
    profiles = getattr(app, 'host', app).meta('report-profiles', {})
    profile = profiles.get(server) if isinstance(profiles, dict) else None
    if (not matches(profile, server, kind) or profile.get('account_id') != account
            or not isinstance(profile.get('instructions'), str) or not 0 < len(profile['instructions']) <= 6000):
        return None
    return dict(profile)


def matches(profile, server, kind):
    return (isinstance(profile, dict) and profile.get('id') == PROFILE_ID and profile.get('version') == 1
            and profile.get('server') == server and kind in ('daily', 'member'))


def marker(profile):
    return {key:profile[key] for key in ('id', 'version', 'server')}
