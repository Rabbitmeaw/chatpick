import unittest

from grouppulse.discord import normalize, normalize_avatar_url


class AvatarTests(unittest.TestCase):
    def test_only_discord_avatar_resources_are_accepted(self):
        url='https://cdn.discordapp.com/avatars/123/'+'a'*32+'.png?size=64'
        self.assertEqual(normalize_avatar_url(url),url)
        for bad in ('https://evil.example/avatar.png','javascript:alert(1)',
                    'https://cdn.discordapp.com.evil.example/avatars/123/a.png',
                    'https://cdn.discordapp.com/attachments/123/456/private.png',
                    'https://cdn.discordapp.com/avatars/123/'+ 'a'*32+'.png?token=private'):
            self.assertEqual(normalize_avatar_url(bad),'')
        self.assertEqual(normalize_avatar_url(None),'')

    def test_avatar_preserves_author_message_timestamp_and_reply_identity(self):
        row=dict(id='500',channel_id='200',timestamp='2026-10-21T01:23:00+00:00',content='合成发言',
                 author=dict(id='900',username='同名',avatar='b'*32),
                 message_reference=dict(message_id='499',channel_id='200'))
        value=normalize(row,'100','200')
        self.assertIn('/avatars/900/',value['avatar_url'])
        self.assertEqual(value['author_id'],'900');self.assertEqual(value['id'],'500')
        self.assertEqual(value['reference_id'],'499');self.assertEqual(value['created'],row['timestamp'])
        row['author']['avatar']=None
        self.assertNotIn('avatar_url',normalize(row,'100','200'))


if __name__=='__main__': unittest.main()
