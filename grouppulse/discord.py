"""Discord REST只读客户端；没有POST/PATCH/PUT/DELETE或Gateway发送入口。"""

import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from .network import ServiceError, http_json

API = 'https://discord.com/api/v10'
READ_BITS = (1 << 10) | (1 << 16)


def normalize_avatar_url(value):
    if not isinstance(value,str) or len(value)>1024: return ''
    pattern = (r'https://(?:cdn\.discordapp\.com|media\.discordapp\.net)/'
               r'(?:avatars/[0-9]+/(?:a_)?[a-fA-F0-9]{32}|embed/avatars/[0-9]+'
               r'|guilds/[0-9]+/users/[0-9]+/avatars/(?:a_)?[a-fA-F0-9]{32})'
               r'\.(?:png|gif|webp|jpg)(?:\?size=(?:16|32|64|128|256|512|1024|2048|4096))?')
    return value if re.fullmatch(pattern,value) else ''


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,22}', value):
        raise ValueError('服务器、频道或消息编号无效，请从列表重新选择。')
    return value


def snowflake(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    return max(0, int(stamp.timestamp() * 1000) - 1420070400000) << 22


def permissions(guild, member, roles, channel, user_id):
    selected = set(member['roles']) | {guild}
    value = 0
    for role in roles:
        if role['id'] in selected:
            value |= int(role['permissions'])
    if value & 8:
        return value | READ_BITS | (1 << 11)
    overrides = channel.get('permission_overwrites', [])
    for rule in overrides:
        if rule['type'] == 0 and rule['id'] == guild:
            value = (value & ~int(rule['deny'])) | int(rule['allow'])
    allow = deny = 0
    for rule in overrides:
        if rule['type'] == 0 and rule['id'] in set(member['roles']):
            allow |= int(rule['allow']); deny |= int(rule['deny'])
    value = (value & ~deny) | allow
    for rule in overrides:
        if rule['type'] == 1 and rule['id'] == user_id:
            value = (value & ~int(rule['deny'])) | int(rule['allow'])
    return value


class DiscordClient:
    def __init__(self, token, base_url=API):
        if not token or not isinstance(token, str) or any(ch.isspace() for ch in token):
            raise ValueError('请先保存有效的 Bot Token，只填写 Token 本身。')
        self.token, self.base = token, base_url.rstrip('/')
        self.authorization = 'Bot ' + token

    def get(self, path, params=None, *, before_read=None):
        patterns = (r'/users/@me', r'/users/@me/guilds', r'/applications/@me',
                    r'/guilds/[0-9]+/(channels|roles)', r'/guilds/[0-9]+/members/[0-9]+',
                    r'/guilds/[0-9]+/threads/active',
                    r'/channels/[0-9]+/(?:threads/archived/public|users/@me/threads/archived/private)',
                    r'/channels/[0-9]+', r'/channels/[0-9]+/messages(?:/[0-9]+)?')
        if not any(re.fullmatch(pattern, path) for pattern in patterns):
            raise ValueError('该操作不在 Discord 只读范围内。')
        for attempt in range(3):
            if before_read is not None:
                before_read()
            try:
                return http_json(self.base + path + ('?' + urlencode(params) if params else ''),
                                 {'Authorization':self.authorization}, timeout=30)
            except ServiceError as error:
                if error.status != 429 or attempt == 2:
                    raise
                time.sleep(error.retry_after or 1)

    def identity(self):
        user = self.get('/users/@me')
        app = self.get('/applications/@me')
        if not isinstance(user, dict) or user.get('bot') is not True:
            raise ValueError('这里只接受 Bot Token，不能使用个人账号 Token。')
        identifier(user.get('id'))
        if not isinstance(app, dict):
            raise ValueError('Discord应用信息响应异常，请重新连接。')
        try:
            flags = int(app.get('flags_new', app.get('flags', 0)))
        except (ValueError, TypeError):
            raise ValueError('Discord应用权限信息响应异常，请重新连接。') from None
        return dict(user_id=user['id'], username=user.get('global_name') or user.get('username', 'Bot'),
                    application_id=identifier(app.get('id')), message_content=bool(flags & ((1 << 18) | (1 << 19))))

    def guilds(self):
        found, after = {}, ''
        while True:
            page = self.get('/users/@me/guilds', dict(limit=200, **({'after':after} if after else {})))
            if not isinstance(page, list):
                raise ValueError('Discord服务器列表响应异常，请重新连接。')
            if not page:
                break
            for guild in page:
                if not isinstance(guild, dict):
                    raise ValueError('Discord服务器列表响应异常，请重新连接。')
                found[identifier(guild.get('id'))] = guild.get('name', '未命名服务器')
            cursor = max(page, key=lambda g:int(g['id']))['id']
            if cursor == after:
                raise ValueError('Discord服务器分页未前进，请稍后重试。')
            after = cursor
            if len(page) < 200:
                break
        return found

    def readable_channels(self, guild, user_id):
        guild, user_id = identifier(guild), identifier(user_id)
        member = self.get(f'/guilds/{guild}/members/{user_id}')
        roles = self.get(f'/guilds/{guild}/roles')
        channels = self.get(f'/guilds/{guild}/channels')
        if not isinstance(channels, list) or not isinstance(roles, list) or not isinstance(member, dict) or not isinstance(member.get('roles'), list):
            raise ValueError('无法核验 Bot 的频道读取权限，请重新连接。')
        try:
            return {identifier(c['id']): c.get('name', '未命名频道') for c in channels if c.get('type') in (0, 5)
                    and (permissions(guild, member, roles, c, user_id) & READ_BITS) == READ_BITS}
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ValueError('Discord频道权限响应异常，请重新发现频道。') from None

    def check_channel(self, guild, channel, user_id):
        guild, channel = identifier(guild), identifier(channel)
        value = self.get(f'/channels/{channel}')
        if not isinstance(value,dict) or value.get('guild_id') != guild or value.get('type') not in (0, 5):
            raise ValueError('频道不属于所选服务器，或不是受支持的文字频道。')
        if channel not in self.readable_channels(guild, user_id):
            raise ValueError('Bot缺少该频道的查看或读取历史权限，请在Discord中授予后重新发现频道。')
        return value

    def history(self, channel, before):
        page = self.get(f'/channels/{identifier(channel)}/messages', dict(limit=100, before=str(before)))
        if not isinstance(page, list):
            raise ValueError('Discord聊天分页响应异常，请重试同步。')
        return page

    def message(self, channel, message_id):
        return self.get(f'/channels/{identifier(channel)}/messages/{identifier(message_id)}')

    def active_threads(self, guild):
        return self.get(f'/guilds/{identifier(guild)}/threads/active')

    def archived_threads(self, channel, private=False, before=''):
        route='users/@me/threads/archived/private' if private else 'threads/archived/public'
        return self.get(f'/channels/{identifier(channel)}/{route}',dict(limit=100,**({'before':before} if before else {})))

    def check_thread(self, guild, parent, thread):
        value=self.get(f'/channels/{identifier(thread)}')
        if (not isinstance(value,dict) or value.get('guild_id')!=guild or value.get('parent_id')!=parent
                or value.get('id')!=thread or value.get('type') not in (10,11,12)):
            raise ValueError('子线程不属于所选服务器和频道，已停止读取。')
        return value


def normalize(row, guild, channel, *, source_channel=None, thread_name=''):
    actual=source_channel or channel
    if not isinstance(row, dict) or row.get('channel_id') != actual or row.get('guild_id',guild)!=guild:
        raise ValueError('收到范围不匹配的消息，已停止同步。')
    author = row['author']
    created = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
    reference = row.get('message_reference') or {}
    reference_id = reference.get('message_id')
    if reference_id:
        identifier(reference_id)
    content = row.get('content', '')
    if not isinstance(content, str):
        raise ValueError('Discord消息正文格式异常，已停止同步。')
    result = dict(id=identifier(row.get('id')), server=guild, channel=channel, created=created,
                author_id=identifier(author.get('id')), name=(row.get('member') or {}).get('nick') or author.get('global_name') or author.get('username', '未命名成员'),
                bot=bool(author.get('bot')), content=content, stance='发言', topic='unclassified', thread=actual,
                reference_id=reference_id, reference_channel=reference.get('channel_id', actual),
                text_available=bool(content.strip()),
                attachment_names=[a.get('filename','附件') for a in row.get('attachments', [])],
                has_embeds=bool(row.get('embeds')), edited_at=row.get('edited_timestamp'),
                url=f'https://discord.com/channels/{guild}/{actual}/{row["id"]}')
    avatar=author.get('avatar')
    if isinstance(avatar,str):
        avatar_url=normalize_avatar_url(f'https://cdn.discordapp.com/avatars/{result["author_id"]}/{avatar}.png?size=64')
        if avatar_url: result['avatar_url']=avatar_url
    if actual!=channel:
        result.update(source_channel=actual,thread_name=thread_name)
    if row.get('type',0) not in (0,19):
        if not isinstance(row['type'],int) or isinstance(row['type'],bool):
            raise ValueError('Discord消息类型异常，已停止同步。')
        result['message_type']=row['type']
    return result
