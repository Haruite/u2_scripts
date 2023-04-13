"""将 VCB 的公网种子文件夹改名，命名格式为: [中文标题][日文标题][日语标题罗马音][VCB和其他字幕组标识][其他]"""
# 种子文件从 nyaa.si 下载，VCB 有部分种子没发布在 nyaa，无法匹配
# 脚本可能有问题，建议先硬链备份
import base64
import json
import os
import re
from time import sleep

import qbittorrentapi
import requests
from bs4 import BeautifulSoup, NavigableString
from loguru import logger
from my_bencoder import bdecode


host = 'localhost'
port = 8080
username = ''
password = ''
proxies = {'http': 'http://127.0.0.1:10809', 'https': 'http://127.0.0.1:10809'}
headers = {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                         'Chrome/112.0.0.0 Safari/537.36 Edg/112.0.1722.39'}
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
jap = re.compile(r'[\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7A3]')
chn = re.compile(u"[\u4e00-\u9fa5]+")
src_path = 'H:\\Ani'


class VCB:
    def __init__(self, save_path='VCB.json', torrents_dir='VCB_torrents'):
        self.to_info = {}
        self.save_path = save_path
        self.torrents_dir = torrents_dir
        self.client = qbittorrentapi.Client(host=host, port=port, username=username, password=password)
        self.hashes = {torrent.hash for torrent in self.client.torrents_info(status_filter='completed')}

    def fetch_title(self):
        i = 1
        while True:
            page = requests.get(f'https://share.dmhy.org/topics/list/team_id/581/page/{i}',
                                headers=headers, proxies=proxies).text
            logger.info(f'Download page: https://share.dmhy.org/topics/list/team_id/581/page/{i}')
            i += 1
            soup = BeautifulSoup(page, 'lxml')
            table = soup.find('table', {'class': 'tablesorter', 'id': 'topic_list'})
            if not table:
                break
            for tr in table.tbody:
                if not isinstance(tr, NavigableString):
                    title = tr.find('td', {'class': 'title'}).contents[3].text.strip()
                    magnet_url = tr.find('a', {'class': 'download-arrow arrow-magnet'})['href']
                    info_hash = base64.b32decode(magnet_url[20: 52].encode()).hex()
                    self.to_info[info_hash] = {'title': title}

    def fix_title(self, info_hash):
        title = self.to_info[info_hash]['title']
        try:
            i1 = title.index(']')
        except:
            return
        pre = title[1:i1].strip()
        try:
            i2 = title.index('[', i1)
            suf = title[i2 + 1: -1].replace(']', '').replace('[', '').strip()
        except:
            try:
                i2 = title.index('(', i1)
                suf = title[i2 + 1: -1].replace(']', '').replace('[', '').strip()
            except:
                sufs = []
                for su in '720p', '1080p', 'HEVC', 'AVC', 'BDrip', '10bit', '8bit':
                    if su in title:
                        sufs.append(su)
                        title = title.replace(su, '').strip()
                suf = ' '.join(su) if sufs else ''
                i2 = len(title)
        name = title[i1 + 1:i2].strip()
        if not name:
            return
        names = name.split('/')
        new_titles = []
        for nm in names:
            if any(chn.search(c) and not jap.search(c) for c in nm):
                new_titles.append(nm.strip())
        for nm in names:
            if nm.strip() not in new_titles:
                if all(not jap.search(c) for c in nm):
                    new_titles.append(nm.strip())
        for nm in names:
            if nm.strip() not in new_titles:
                new_titles.append(nm.strip())
        new_titles.append(suf)
        new_titles.append(pre)
        new_title = '[' + ']['.join(new_titles) + ']'
        new_title = ''.join(char_map.get(char) or char for char in new_title)
        self.to_info[info_hash]['title'] = new_title

    def fetch_torrents(self):
        for _ in range(1, 11):
            page = requests.get(f'https://nyaa.si/user/VCB-Studio?p={_}', headers=headers, proxies=proxies).text
            logger.info(f'Downloaded page: https://nyaa.si/user/VCB-Studio?p={_}')
            soup = BeautifulSoup(page, 'lxml')
            table = soup.find('table', {'class': 'table table-bordered table-hover table-striped torrent-list'})
            for tr in table.tbody:
                if not isinstance(tr, NavigableString):
                    td = tr.contents[5]
                    dl_link = 'https://nyaa.si' + td.contents[1]['href']
                    info_hash = td.contents[3]['href'][20:60]
                    torrent_path = f'{self.torrents_dir}\\{info_hash}.torrent'

                    if os.path.exists(torrent_path):
                        logger.debug(f'.torrent file already exits. Info hash: {info_hash}')
                        with open(torrent_path, 'rb') as f:
                            content = f.read()
                    else:
                        content = requests.get(dl_link, headers=headers, proxies=proxies).content
                        with open(torrent_path, 'wb') as f:
                            f.write(content)
                        logger.info(f'Downloaded torrent, info hash {info_hash}')

                    name = bdecode(content)[b'info'][b'name'].decode()
                    if info_hash in self.to_info:
                        self.to_info[info_hash]['name'] = name
                    else:
                        self.to_info[info_hash] = {'name': name}
            self.save_info()

    def save_info(self):
        with open(self.save_path, 'w') as fp:
            json.dump(self.to_info, fp)

    def run_job(self):
        if not os.path.exists(self.save_path):
            with open(self.save_path, 'a'):
                pass
            self.fetch_title()
            for info_hash in self.to_info:
                self.fix_title(info_hash)
            self.save_info()
        else:
            with open(self.save_path, 'r') as fp:
                self.to_info = json.load(fp)
        if not os.path.exists(self.torrents_dir):
            os.mkdir(self.torrents_dir)
        self.fetch_torrents()
        name_to_hash = {data['name']: _hash for _hash, data in self.to_info.items() if 'name' in data}
        for fn in os.listdir(src_path):
            if 'VCB' in fn:
                if fn in name_to_hash:
                    _hash = name_to_hash[fn]
                    logger.info(f'Found torrent: {fn} -> {_hash}')
                    if _hash in self.hashes:
                        logger.info(f'Torrent already in client: {fn}')
                    else:
                        with open(f'{self.torrents_dir}\{_hash}.torrent', 'rb') as f:
                            content = f.read()
                        try:
                            self.client.torrents_add(torrent_files=content, save_path=src_path, is_paused=True)
                            sleep(0.1)
                        except Exception as e:
                            logger.error(e)
                    data = self.to_info[_hash]
                    if data.get('title'):
                        try:
                            self.client.torrents_rename(_hash, data['title'])
                            logger.info(f"成功重命名种子名称 {fn} -> {data['title']}")
                            os.renames(f'{src_path}\\{fn}', f"{src_path}\\{data['title']}")
                            self.client.torrents_rename_folder(_hash, fn, data['title'])
                        except Exception as e:
                            logger.error(e)
                else:
                    logger.warning(f'Could not find torrent: {fn}')


if __name__ == '__main__':
    VCB().run_job()
