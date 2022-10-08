"""给指定的多个种子放魔法，直接异步发 post 请求，没有任何多余步骤."""

import asyncio
import re
from typing import Union, Dict, Tuple, List
from datetime import datetime, timedelta

import aiohttp
import pytz

cookies = {'nexusphp_u2': ''}  # type: Dict[str, str]
'''网站 cookie'''
proxy = ''  # type: Union[str, None]
'''代理'''
torrent_ids = 32769, 32692, 32646  # type: Union[Tuple[Union[str, int], ...], List[Union[str, int]]]
'''种子 id，多个用逗号隔开'''
ur = 2.33  # type: Union[int, float, str]
'''上传比率'''
dr = 0  # type: Union[int, float, str]
'''下载比率'''
user = 'ALL'  # type: str
"""'ALL', 'SELF' 和 'OTHER'"""
user_other = ''  # type: Union[int, str]
'''有效用户的 uid, 仅当 user 为 "OTHER" 时有效'''
hours = 120  # type: Union[int, str]
'''持续时间(小时)'''
comment = ''  # type: str
'''评论'''
delay = 0  # type: int
'''魔法生效延时秒数，大于 0 时地图炮魔法不会出现在群聊区'''
timezone = 'Asia/Shanghai'  # type: str
'''时区(网站页面右上角)，仅当 delay 不为 0 时需要使用'''
r_args = {
    'cookies': cookies,
    'headers': {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/105.0.0.0 Safari/537.36 Edg/105.0.1343.53'
    },
    'proxy': proxy,
}
'''aiohttp 模块 request 函数参数'''


class Magic:
    def __init__(self):
        self.session = None
        self.torrent_ids = self.get_tid()
        self.start = (
                datetime.now().astimezone(pytz.timezone(timezone)) + timedelta(seconds=delay)
        ).strftime('%Y-%m-%d %H:%M:%S') if delay != 0 else 0

    @staticmethod
    def get_tid() -> Union[Tuple[Union[str, int], ...], List[Union[str, int]]]:
        return torrent_ids

    async def run(self):
        magic_tasks = [self.magic(tid) for tid in self.torrent_ids]
        async with aiohttp.ClientSession() as self.session:
            res = await asyncio.gather(*magic_tasks)
        for i, msg in enumerate(res):
            state = '成功' if re.match(r'^<script.+<\/script>$', msg) else '失败'
            print(f'种子{self.torrent_ids[i]}施加魔法{state}')

    async def magic(self, tid):
        data = {
            'action': 'magic', 'divergence': '', 'base_everyone': '', 'base_self': '', 'base_other': '',
            'torrent': tid, 'tsize': '', 'ttl': '', 'user': user, 'hours': hours, 'ur': ur, 'dr': dr,
            'user_other': user_other, 'start': self.start, 'promotion': 8, 'comment': comment
        }
        async with self.session.post(f"https://u2.dmhy.org/promotion.php", **r_args, data=data) as resp:
            return await resp.text()


asyncio.run(Magic().run())
