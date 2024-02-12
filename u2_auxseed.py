"""
将其他站的种子或者公网种子的source字段改为u2专用的
并且重新计算info_hash，如果匹配就下载对应种子辅种
适用于没用重新制种用转钟脚本转载的种子
"""
import asyncio
import os
from hashlib import sha1

import aiohttp
import qbittorrentapi
from loguru import logger

from my_bencoder import bencode, bdecode

host = 'localhost'  # ip
port = 8080  # webui 端口
username = ''  # webui 用户名
password = ''  # webui 密码
bt_backup = 'C:\\Users\\XXX\\AppData\\Local\\qBittorrent\\BT_backup'
# qb 的备份文件夹(存放种子)，此项必填，linux 下默认路径是 用户目录/.local/share/qBittorrent/BT_backup
passkey = ''  # 下载种子用
token = ''
# u2 api(第三方): https://github.com/kysdm/u2_api 的 token, 自动获取 token: https://greasyfork.org/zh-CN/scripts/428545
uid = 50096
proxy = ''  # ‘http://127.0.0.1:10809’  # 可不填

logger.add(level='DEBUG', sink=f'{os.getcwd()}\\logs\\auto_seed-{{time}}.log')
client = qbittorrentapi.Client(host=host, port=port, username=username, password=password)


async def aux_seed(torrent: qbittorrentapi.TorrentDictionary, session: aiohttp.ClientSession, sem: asyncio.Semaphore):
    if 'daydream.dmhy.best' not in torrent.magnet_uri:
        info_dict = bdecode(bt_backup + '/' + torrent.hash + '.torrent')[b'info']
        info_dict[b'source'] = '[u2.dmhy.org] U2分享園@動漫花園'
        torrent_hash = sha1(bencode(info_dict)).hexdigest()
        params = {'uid': uid, 'token': token, 'hash': torrent_hash}

        async with sem:
            async with session.get('https://u2.kysdm.com/api/v1/history', params=params, proxy=proxy) as resp:
                _json = await resp.json()

        history = _json['data']['history']
        if history:
            torrent_id = history[0]['torrent_id']
            logger.info(f'找到种子 {torrent_hash} {torrent.name}，U2 种子 id 为 {torrent_id}, 将尝试辅种')
            async with sem:
                async with session.get(
                        f'https://u2.dmhy.org/download.php?id={torrent_id}&passkey={passkey}&https=1',
                        params=params, proxy=proxy
                ) as resp:
                    content = await resp.read()
                    client.torrents_add(
                        torrent_files=content, save_path=os.path.split(torrent.content_path)[0], is_paused=True
                    )
                    logger.info(f'已添加种子 {torrent_hash} {torrent.name}')
        else:
            logger.debug(f'未找到种子 {torrent_hash} {torrent.name}，api 未返回种子信息')


async def main():
    sem = asyncio.Semaphore(20)
    async with aiohttp.ClientSession() as session:
        tasks = (aux_seed(torrent, session, sem) for torrent in client.torrents_info(status_filter='completed'))
        await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
