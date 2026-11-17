"""StarMemo 当前可选版式的产品清单；布局由群脉独立实现。

来源与边界见 docs/starmemo-reference.md，不加载外部模板或执行模板脚本。
"""

DEFAULT_LAYOUT = 'v1'
ORDER = ['hero', 'topics', 'actions', 'important']
LAYOUTS = [
    dict(id='v1', name='经典日报', style='classic', platform='mobile',
         description='熟悉的单栏日报，按总览、话题和行动项依次阅读。', order=ORDER),
    dict(id='mobile-feed', name='轻量信息流', style='feed', platform='mobile',
         description='窄栏信息流，以参与者和发言摘录串起讨论。', order=ORDER),
    dict(id='mobile-magazine', name='AI Magazine', style='magazine', platform='mobile',
         description='大标题与编辑式留白，突出头条和话题。', order=ORDER),
    dict(id='mobile-dashboard', name='AI Command Center', style='dashboard', platform='mobile',
         description='先看本次统计、关键决定和行动线索，再读话题。',
         order=['hero', 'actions', 'topics', 'important']),
    dict(id='desktop-workspace', name='三栏 AI 工作台', style='workspace', platform='desktop',
         description='总览、核心讨论、行动项三栏并列，适合电脑阅读。', order=ORDER),
    dict(id='desktop-editorial', name='Editorial 科技日报', style='editorial', platform='desktop',
         description='头条横跨版面，话题与行动分栏阅读。', order=ORDER),
    dict(id='external:community.github.tracememo.paperdaily@1.0.1', name='纸上日报', style='paper', platform='mobile',
         description='纸张色、细分隔线与宋体标题的报纸式单栏。', order=ORDER),
    dict(id='external:community.github.tracememo.quickread@1.0.0', name='极简速读', style='quickread', platform='mobile',
         description='减少装饰，先看结论和行动，再展开讨论。',
         order=['hero', 'actions', 'important', 'topics']),
    dict(id='external:community.github.tracememo.teamboard@1.0.0', name='团队看板', style='teamboard', platform='desktop',
         description='讨论、重要消息、行动项组成三栏协作看板。',
         order=['hero', 'actions', 'topics', 'important']),
    dict(id='external:community.github.wxw-gu.mountain-daily@1.0.0', name='山水协作日报', style='mountain', platform='desktop',
         description='浅蓝山水背景，话题卡片横向排列，协作事项单独展示。', order=ORDER),
    dict(id='external:community.github.wxw-gu.neon-command-daily@1.0.2', name='霓光指挥日报', style='neon', platform='desktop',
         description='深色霓光看板，突出统计、重点和行动。',
         order=['hero', 'important', 'actions', 'topics']),
]
