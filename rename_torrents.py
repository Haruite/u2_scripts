"""
种子文件按照网站标题重命名
"""
import asyncio
import os
import sys

import aiohttp
import qbittorrentapi
from loguru import logger

from my_bencoder import bdecode


mode = 1
# mode = 1: 既重命名 qb 的名称，也重命名文件
# mode = 2: 只重命名 qb 的名称，不重命名文件
# mode = 3: 还原 qb 的名称和实际文件名
# mode = 4: 只还原实际文件名
# mode = 5: 只还原 qb 的名称
host = 'localhost'  # ip
port = 8080  # webui 端口
username = ''  # 用户名
password = ''  # 密码
src_path = r'E:\Lossless Music'
# 包含种子的文件夹，注意 Windows 下格式是反斜杠，linux 是正斜杠，否则不能匹配
# 如果为空则匹配所有种子，另外如果种子在子文件夹则不改名，因为可能影响做种
bt_backup = r'C:\Users\XXX\AppData\Local\qBittorrent\BT_backup'
# qb 的备份文件夹(存放种子)，将 xxx 改为 Windows 用户名，如果不填则每次都需要从 api 获取种子信息
token = ''
# u2 api: https://github.com/kysdm/u2_api 的 token, 自动获取 token: https://greasyfork.org/zh-CN/scripts/428545
uid = 50096  # 自己的 uid
proxy = ''  # 'http://127.0.0.1:10809'  # 代理，可不填
char_map = {
    '?': '？',
    '*': '★',
    '<': '《',
    '>': '》',
    ':': '：',
    '"': "'",
    '/': '／',
    '\\': '／',
    '|': '￨'
}  # Windows 文件名不支持的字符，冒号后为需要替换成的字符
os_rename = False
# 是否直接通过 os 重命名文件夹，如果使用qb重命名文件夹则不属于种子的部分不会移动到新文件夹
# 如果是 BDrip 里面包含有外挂字幕(不属于种子内容)，建议改为 True
# 如果是无损音乐或者外挂结构，建议 False


logger.add(level='DEBUG', sink=f'{os.getcwd()}\\logs\\rename_torrents-{{time}}.log')
client = qbittorrentapi.Client(host=host, port=port, username=username, password=password)
if mode not in range(6):
    logger.error('未知 mode')
    exit()


async def rename_torrent(torrent: qbittorrentapi.TorrentDictionary,
                         session: aiohttp.ClientSession, sem: asyncio.Semaphore):
    if 'daydream.dmhy.best' in torrent.magnet_uri and (
        not src_path or torrent.save_path == src_path
        or src_path + '/' == torrent.save_path
        or src_path + '\\' == torrent.save_path
    ):
        params = {'uid': uid, 'token': token, 'hash': torrent.hash}
        history = []
        try:
            old_path = torrent.content_path[len(torrent.save_path):].split('\\' if sys.platform == 'win32' else '/')[1]
        except:
            old_path = torrent.name
        old_name = torrent.name
        info_dict = {}
        try:
            info_dict = bdecode(bt_backup + '\\' + torrent.hash + '.torrent')[b'info']
            origin_name = info_dict[b'name'].decode()
            origin_name = ''.join(char_map.get(char) or char for char in origin_name)
        except Exception as e:
            logger.exception(e)
            async with sem:
                async with session.get('https://u2.kysdm.com/api/v1/history', params=params, proxy=proxy) as resp:
                    _json = await resp.json()
                    history = _json['data']['history']
                    if not history:
                        logger.warning(f'未找到种子 {torrent.hash} {old_name}，api 未返回种子信息')
                        return
                    origin_name = history[0]['torrent_name']
                    origin_name = ''.join(char_map.get(char) or char for char in origin_name)
        new_name = origin_name

        if mode in (1, 3, 4) and not os.path.exists(torrent.content_path):
            logger.warning(f'意外的错误，种子 {torrent.hash} {old_name} 文件不存在')
            return
        if mode in (1, 2) and old_name != origin_name:
            logger.debug(f'种子 {torrent.hash} {old_name} 已经改名')
            return
        if mode == 1 and old_path != origin_name:
            logger.debug(f'种子 {torrent.hash} {old_name} 文件名已更改')
            return
        if mode in (3, 4) and old_path == origin_name:
            logger.debug(f'种子 {torrent.hash} {old_name} 不需要还原文件名')
            return
        if mode in (3, 5) and old_name == origin_name:
            logger.debug(f'种子 {torrent.hash} {old_name} 不需要还原 qb 名称')
            return

        if mode in (1, 2):
            if not history:
                async with sem:
                    async with session.get('https://u2.kysdm.com/api/v1/history', params=params, proxy=proxy
                                           ) as resp:
                        _json = await resp.json()
                        history = _json['data']['history']
                        if not history:
                            logger.warning(f'未找到种子 {torrent.hash} {old_name}，api 未返回种子信息')
                            return
            if not history:
                logger.warning(f'未找到种子 {torrent.hash} {torrent.name}，api 未返回种子信息')
                return
            title = ''.join(char_map.get(char) or char for char in history[0]['title'])
            if os.path.isdir(torrent.content_path):
                new_name = title
            else:
                new_name = title + os.path.splitext(origin_name)[1]
                if info_dict and info_dict.get(b'files') and len(info_dict[b'files']) == 1:
                    new_name = title + os.path.splitext(info_dict[b'files'][0][b'path'][0].decode())[1]

        if mode == 1 and origin_name == new_name:
            logger.debug(f'种子 {torrent.hash} {old_name} 不需要改名')
            return

        if mode in (1, 2, 3, 5):
            try:
                torrent.rename(new_name)
            except Exception as e:
                logger.error(f'重命名种子名称 {old_path} -> {new_name} 失败，原因 {e}')
            else:
                logger.info(f'成功重命名种子名称 {old_path} -> {new_name}')

        if mode in (1, 3, 4):
            if os.path.isdir(torrent.content_path):
                try:
                    if os_rename:
                        os.renames(src_path + '/' + old_path, src_path + '/' + new_name)
                    torrent.rename_folder(old_path, new_name)
                except Exception as e:
                    logger.error(f'重命名种子文件夹 {old_path} -> {new_name} 失败，原因 {e}')
                else:
                    logger.info(f'成功重命名种子文件夹 {old_path} -> {new_name}')
            else:
                try:
                    torrent.rename_file(0, new_name)
                except Exception as e:
                    logger.trace(f'重命名种子文件 {old_path} -> {new_name} 失败，原因 {e}')
                else:
                    logger.info(f'成功重命名种子文件 {old_path} -> {new_name}')


async def main():
    sem = asyncio.Semaphore(20)
    async with aiohttp.ClientSession() as session:
        tasks = (rename_torrent(torrent, session, sem) for torrent in client.torrents_info(status_filter='completed'))
        await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
