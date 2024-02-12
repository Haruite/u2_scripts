"""
按指定规则重名文件夹里的所有.torrent文件
"""
import asyncio
import os
import shutil
from hashlib import sha1

import aiohttp
from loguru import logger

from my_bencoder import bencode, bdecode

filename_format = '[{cat}][{size}][{tid}] {title} - {uploader} - {uptime}'
src_path = r'C:\Backup\torrents'
dst_path = r'C:\Backup\to'
token = ''
uid = 50096
proxy = ''
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
}


async def rename_file(filename, info_hash, session, sem):
    async with sem:
        params = {'uid': uid, 'token': token, 'hash': info_hash}
        async with session.get('https://u2.kysdm.com/api/v1/history', params=params, proxy=proxy) as resp:
            json_data = await resp.json()
            history = json_data['data']['history']
            if history:
                torrent = history[0]
                name = filename_format
                name = name.replace('{hash}', info_hash)
                up_time = torrent['uploaded_at']
                name = name.replace('{uptime}', up_time)
                torrent_name = torrent['torrent_name']
                name = name.replace('{name}', torrent_name)
                cat = torrent['category']
                name = name.replace('{cat}', cat)
                tid = torrent['torrent_id']
                name = name.replace('{tid}', str(tid))
                title = torrent['title']
                name = name.replace('{title}', title)
                uploader = torrent['uploader_id']
                name = name.replace('{uploader}', str(uploader))
                size = torrent['torrent_size']
                name = name.replace('{size}', str(size))
                name = ''.join(char_map.get(c) or c for c in name)
                try:
                    shutil.copyfile(src_path + '/' + filename, dst_path + '/' + name + '.torrent')
                except Exception as e:
                    logger.error(e)
            else:
                logger.debug(f'{info_hash} {filename} api 未返回种子信息')


async def main():
    hashes = set()
    for filename in os.listdir(dst_path):
        hashes.add(sha1(bencode(bdecode(dst_path + '/' + filename)[b'info'])).hexdigest())

    sem = asyncio.Semaphore(20)
    tasks = []
    async with aiohttp.ClientSession() as session:
        for filename in os.listdir(src_path):
            info_hash = sha1(bencode(bdecode(src_path + '/' + filename)[b'info'])).hexdigest()
            if info_hash not in hashes:
                tasks.append(rename_file(filename, info_hash, session, sem))
        await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
