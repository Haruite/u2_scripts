import gc
import os
import re
import shutil
import pytz

from collections import deque
from datetime import datetime
from time import sleep, time
from requests import get
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from loguru import logger

RUN_CRONTAB = False  # if true, that means you run crontab job
R_ARGS = {'headers': {'cookie': 'nexusphp_u2=',  # your cookie is required
                      'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.81 Safari/537.36'
                      },
          'timeout': 20
          }  # arguments for requests module
BK_DIR = '/root/backup'  # directory which stores torrents for backup
WT_DIR = '/de/wt'  # watch dir of BT client
INTERVAL = 120  # interval when check magic
MAX_SEEDER_NUM = 5  # max seeder number if download torrent
LOG_PATH = f'{os.path.splitext(__file__)[0]}.log'
DATA_PATH = f'{os.path.splitext(__file__)[0]}.data.txt'
DOWNLOAD_ON_FIRST_TIME = False  # if false, download all torrents at first time, or don't download
# only effective when set RUN_CRONTAB False
DOWNLOAD_NON_FREE = False  # if false, download free torrents only, or download free and no-free torrent
MIN_DAY = 7  # min days after torrent released to avoid download new torrent


class CatchMagic:
    def __init__(self):
        logger.add(level='DEBUG', sink=LOG_PATH, rotation='2 MB')
        self.checked = deque([], maxlen=200)
        self.first_time = 1 - DOWNLOAD_ON_FIRST_TIME
        with open(DATA_PATH, 'a', encoding='utf-8'):
            pass
        with open(DATA_PATH, 'r', encoding='utf-8') as f:
            fr = f.read()
            if fr.startswith('checked = '):
                self.checked = eval(fr[len('checked = '):])

    def all_effective_magic(self):
        page = get('https://u2.dmhy.org/promotion.php?action=list&effectonly=1', **R_ARGS).text
        soup = BeautifulSoup(page.replace('\n', ''), 'lxml')
        for i, tr in filter(lambda tup: tup[0] > 0, enumerate(soup.find('table', {'width': '99%'}))):
            if tr.contents[1].string in ['魔法', 'Magic', 'БР']:
                if tr.contents[2].a:
                    if not tr.contents[3].a and tr.contents[3].string in ['所有人', 'Everyone', 'Для всех']:
                        tid = tr.contents[2].a['href'][15:]
                        magic_id = tr.contents[0].string
                        if magic_id not in self.checked:
                            if self.first_time and not RUN_CRONTAB:
                                self.checked.append(magic_id)
                            else:
                                yield magic_id, tid
        if self.first_time and not RUN_CRONTAB:
            with open(f'{DATA_PATH}', 'w', encoding='utf-8') as f:
                f.write(f'checked = {self.checked}\n')
        self.first_time = False

    @staticmethod
    def dl_to(to_name, dl_link):
        with open(f'{BK_DIR}/{to_name}', 'wb') as f:
            f.write(get(dl_link, **R_ARGS).content)
        shutil.copy(f'{BK_DIR}/{to_name}', f'{WT_DIR}/{to_name}')
        tid = dl_link.split('&passkey')[0].split('id=')[1]
        logger.info(f'Download torrent {tid}, name {to_name}')

    @staticmethod
    def get_pro(td):
        pro = {'ur': 1.0, 'dr': 1.0}
        pro_dict = {'free': {'dr': 0.0}, '2up': {'ur': 2.0}, '50pct': {'dr': 0.5}, '30pct': {'dr': 0.3}, 'custom': {}}
        for img in td.select('img') or []:
            if not [pro.update(data) for key, data in pro_dict.items() if key in img['class'][0]]:
                pro[{'arrowup': 'ur', 'arrowdown': 'dr'}[img['class'][0]]] = float(img.next.text[:-1].replace(',', '.'))
        return list(pro.values())

    def analyze_magic(self, magic_id, tid):
        page = get(f'https://u2.dmhy.org/details.php?id={tid}', **R_ARGS).text
        logger.debug(f'Download page: https://u2.dmhy.org/details.php?id={tid}')
        self.checked.append(magic_id)
        with open(f'{DATA_PATH}', 'w', encoding='utf-8') as f:
            f.write(f'checked = {self.checked}\n')
        soup = BeautifulSoup(page.replace('\n', ''), 'lxml')

        tz_info = soup.find('a', {'href': 'usercp.php?action=tracker#timezone'})['title']
        pre_suf = [['时区', '，点击修改。'], ['時區', '，點擊修改。'], ['Current timezone is ', ', click to change.']]
        tz = [tz_info[len(pre):-len(suf)].strip() for pre, suf in pre_suf if tz_info.startswith(pre)][0]
        date = soup.time.get('title') or soup.time.text
        dt = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')
        delta = time() - pytz.timezone(tz).localize(dt).timestamp()

        if delta < MIN_DAY * 86400:
            logger.debug(f'Torrent {tid} | time < {MIN_DAY} days')
            return
        if not DOWNLOAD_NON_FREE:
            if [self.get_pro(tr.contents[1])[1] for tr in soup.find('table', {'width': '90%'})
                    if tr.td.text in ['流量优惠', '流量優惠', 'Promotion', 'Тип раздачи (Бонусы)']][0] > 0:
                logger.debug(f'Torrent {tid} | is not free')
                return

        seeder_count = int(re.search(r'(\d+)', soup.find('div', {'id': 'peercount'}).b.text).group(1))
        aa = soup.select('a.index')
        to_name = aa[0].text
        dl_link = f"https://u2.dmhy.org/{aa[1]['href']}"
        if seeder_count > 0:
            if seeder_count <= MAX_SEEDER_NUM:
                self.dl_to(to_name, dl_link)
            else:
                m_page = get(f'https://u2.dmhy.org/promotion.php?action=detail&id={magic_id}', **R_ARGS).text
                logger.debug(f'Download page: https://u2.dmhy.org/promotion.php?action=detail&id={magic_id}')
                soup1 = BeautifulSoup(m_page.replace('\n', ''), 'lxml')
                comment = soup1.legend.parent.contents[1].text
                if '搭' in comment and '桥' in comment or '加' in comment and '速' in comment:
                    user = soup1.select('table.main bdo')[0].text
                    logger.debug(f'torrent {tid} | user {user} is looking for help')
                    self.dl_to(to_name, dl_link)
                else:
                    logger.debug(f'torrent {tid} | seeders > {MAX_SEEDER_NUM}')
        else:
            logger.debug(f'torrent {tid} | no seeders')

    def run(self):
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(self.analyze_magic, magic_id, tid): tid
                       for magic_id, tid in self.all_effective_magic()}
            if futures:
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as er:
                        logger.exception(er)


c = CatchMagic()
if RUN_CRONTAB:
    c.run()
else:
    while True:
        try:
            c.run()
        except Exception as e:
            logger.exception(e)
        finally:
            gc.collect()
            sleep(INTERVAL)
