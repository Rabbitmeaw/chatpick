/* 浏览器复制与后台投递共用；仅排版已校验报告，不生成摘要。 */
function discordTextLines(value, prefix='') {
  const text=String(value ?? '').replace(/<(@[!&]?\d+|#\d+)>/g,(_,mention)=>mention[0]+'\u200b'+mention.slice(1))
    .replace(/@(everyone|here)\b/g,'@\u200b$1').replace(/\r\n?/g,'\n');
  const lines=[]; let line=prefix;
  for (const char of text) {
    if (char === '\n') { lines.push(line); line=prefix; continue; }
    const token=/[\\`*_~|\[\]()<>#]/.test(char) ? '\\'+char : char;
    if (line.length+token.length>1800) { lines.push(line); line=prefix; }
    line+=token;
  }
  lines.push(line); return lines;
}

function serverReportSections(report) {
  const profile=report.report_profile;
  if (!profile || profile.id !== 'us-stock-chat' || profile.version !== 1 || profile.server !== report.server ||
      !['daily','member'].includes(report.kind)) return null;
  const sections=[['stock_opinion','美股个人观点'],['news','消息与见闻'],['chat','闲聊与互动'],['other','其他内容']];
  return sections.map(([key,title])=>({key,title,topics:report.digest.topics.filter(topic=>
    key === 'other' ? !['stock_opinion','news','chat'].includes(topic.section) : topic.section === key)}))
    .filter(section=>section.topics.length);
}

function discordReportParts(report, includeSources=false) {
  const digest=report.digest;
  const names={daily:'日报',weekly:'周报',member:'成员发言摘要',discussion:'多人讨论摘要'};
  const blocks=[], byId=new Map(report.messages.map(row=>[row.id,row]));
  const add=(value,prefix='')=>blocks.push(discordTextLines(value,prefix).join('\n'));
  const sources=ids=>{
    if (!includeSources) return;
    [...new Set(ids || [])].forEach((id,index)=>{
      const row=byId.get(id);
      if (!row || row.server !== report.server || row.channel !== report.channel) return;
      const url=discordSourceURL(row);
      if (url) blocks.push(`[原文 ${index+1}](${url})`);
    });
  };
  add('拾聊 · '+names[report.kind]+(report.subject?.name ? ' · '+report.subject.name : ''),'## ');
  add('来源：'+(report.server_name || bootstrap.servers[report.server] || report.server)+' / #'+
    (report.channel_name || bootstrap.channels[report.channel] || report.channel));
  add('周期：'+report.window.label+'（'+(report.window.timezone || 'Asia/Shanghai')+'）');
  add(`${report.selected_count ?? digest.summaryStats.messageCount} 条发言 · ${digest.summaryStats.activeUsers} 位成员`);
  if (report.kind === 'member') add('仅整理该成员的发言，他人引用仅作背景。','> ');
  if (report.offline) add('合成样例 · 非真实聊天记录。','> ');
  const overview=digest.hero?.summary || digest.overview;
  const groups=serverReportSections(report);
  if ((groups || isPeriodic(report.kind) || report.kind === 'member' && !digest.topics.length) && overview) {
    if (groups) add(digest.overviewStyle === 'topic-index' ? '本期速览' : '今日速览','### ');
    add(overview); sources(digest.hero?.summarySourceMessageIds || digest.overviewSourceMessageIds);
  }
  const appendDigest=(value,periodic=false,grouped=null)=>{
  const appendTopic=topic=>{
    add(topic.title,'### ');
    if (grouped && topic.timeline?.length) {
      topic.timeline.forEach(entry=>{
        add(entry.timeRange+' · '+entry.title,'#### ');
        add(entry.summary);
        if (entry.review?.confidence === 'low') add('置信度较低 · '+(entry.review.reasons || []).join(' '),'> ');
        sources(entry.summarySourceMessageIds || entry.sourceMessageIds);
      });
      return;
    }
    if (grouped && topic.timeRange) add(topic.timeRange);
    add(topic.summary);
    if (topic.review?.confidence === 'low') add('置信度较低 · '+(topic.review.reasons || []).join(' '),'> ');
    sources(topic.summarySourceMessageIds || topic.sourceMessageIds);
    for (const conclusion of grouped ? [] : topic.conclusions || []) {
      add((conclusion.sender ? conclusion.sender+'：' : '')+conclusion.text,'- '); sources(conclusion.sourceMessageIds);
    }
  };
  if (grouped) for (const section of grouped) {
    add(section.title,'## ');
    section.topics.forEach(appendTopic);
  }
  else value.topics.forEach(appendTopic);
  for (const [title,section,field] of [['重要消息','importantMessages','content'],['明确约定','todos','task'],['尚未答复的问题','unresolved','question']]) {
    if (section === 'importantMessages' && (!periodic || grouped)) continue;
    const items=value[section] || [];
    if (!items.length) continue;
    add(title,'### ');
    for (const item of items) {
      add((item.sender || item.asker ? (item.sender || item.asker)+'：' : '')+item[field],'- ');
      if (section === 'todos' && (item.owner || item.deadline)) add([item.owner && '负责人：'+item.owner,item.deadline && '期限：'+item.deadline].filter(Boolean).join(' · '));
      sources(item.sourceMessageIds);
    }
  }
  };
  appendDigest(digest,isPeriodic(report.kind),groups);
  if (isPeriodic(report.kind)) for (const dimension of Object.values(report.dimensions || {})) {
    add('关注维度 · '+names[dimension.kind]+' · '+dimension.subject.name,'### ');
    if (!dimension.digest || dimension.generation?.status === 'failed') add('此摘要未生成，未包含内容。','> ');
    else { appendDigest(dimension.digest,false,serverReportSections({...report,...dimension})); if (dimension.coverage?.note) add('覆盖：'+dimension.coverage.note,'> '); }
  }
  const hiddenSections=groups ? ['topics','todos','unresolved'] : isPeriodic(report.kind) ? ['topics','importantMessages','todos','unresolved'] :
    report.kind === 'member' ? ['topics','todos','unresolved'] : ['todos','unresolved'];
  const hidden=hiddenSections.reduce((sum,key)=>sum+(digest.sectionMeta?.[key]?.hiddenCount || 0),0);
  if (hidden) add(`当前阅读模式有 ${hidden} 个候选条目未展示，复制内容与当前报告一致。`,'> ');
  add('覆盖：'+report.coverage.note,'> ');
  let chunk=''; const chunks=[];
  for (const block of blocks) for (const line of block.split('\n')) {
    const joined=chunk ? chunk+'\n\n'+line : line;
    if (joined.length>1900) { chunks.push(chunk); chunk=line; } else chunk=joined;
  }
  if (chunk) chunks.push(chunk);
  return chunks.length>1 ? chunks.map((part,index)=>`-# 第 ${index+1}/${chunks.length} 段\n${part}`) : chunks;
}

function discordSourceURL(message) {
  const parts = [message.server, message.source_channel || message.channel, message.id];
  if (!parts.every(part => typeof part === 'string' && /^[0-9]{1,22}$/.test(part))) return '';
  return 'https://discord.com/channels/'+parts.join('/');
}

