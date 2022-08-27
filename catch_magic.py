"""必须填写 cookie，之后修改 BK_DIR 和 WT_DIR，即可运行"""

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

RUN_CRONTAB = False  # 如果为真，代表运行一次 run 函数退出，需要以一定间隔运行脚本，主要解决内存问题。否则一直循环运行不退出
R_ARGS = {'headers': {'cookie': 'nexusphp_u2=',  # 填网站 cookie，不要空格
                      'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.81 Safari/537.36'
                      },
          'timeout': 20,
          'proxies': {  # 'http': "127.0.0.1:10809", 'https': "127.0.0.1:10809"
          }
          }  # requests 模块参数
BK_DIR = '/root/backup'  # 备份种子文件夹路径
WT_DIR = '/de/wt'  # BT 客户端监控文件夹
INTERVAL = 120  # 检查魔法的时间间隔
MAX_SEEDER_NUM = 5  # 最大的做种人数，超过不下载
LOG_PATH = f'{os.path.splitext(__file__)[0]}.log'  # 日志文件路径
DATA_PATH = f'{os.path.splitext(__file__)[0]}.data.txt'  # 数据文件路径
DOWNLOAD_ON_FIRST_TIME = False  # 如果为真，第一次下载所有符合要求的种子，否则的话跳过第一次的所有种子，只在 RUN_CRONTAB 为真的时候有效
DOWNLOAD_NON_FREE = False  # 如果为真为下载不是 free 的种子，否则的话只下载 free 的种子
MIN_DAY = 7  # 种子发布时间小于此天数则不下载，避免下载新种
MAGIC_SELF = False  # 如果为真，会下载给自己放魔法的种子，否则不下载
EFFECTIVE_BUFFER = 60  # 如果该魔法是 free 并且生效时间在此之内，就算种子不是 free 也直接下载
DOWNLOAD_DEAD_SEED = False  # 默认不下载无人做种的种子，如果要下载改成 True
RE_DOWNLOAD = True  # 如果为 False，检测到备份文件夹有该种子则不再次下载


class CatchMagic:
    pre_suf = [['时区', '，点击修改。'], ['時區', '，點擊修改。'], ['Current timezone is ', ', click to change.']]

    def __init__(self):
        logger.add(level='DEBUG', sink=LOG_PATH, rotation='2 MB')
        self.checked = deque([], maxlen=200)
        self.first_time = True
        with open(DATA_PATH, 'a', encoding='utf-8'):
            pass
        with open(DATA_PATH, 'r', encoding='utf-8') as f:
            fr = f.read()
            if fr.startswith('checked = '):
                self.checked = eval(fr[len('checked = '):])
        self.magic_id_0 = None

    def all_effective_magic(self):
        all_checked = True if self.first_time else False
        index = 0
        while True:
            soup = self.get_soup(f'https://u2.dmhy.org/promotion.php?action=list&page={index}')
            user_name = soup.find('table', {'id': 'info_block'}).bdo.text
            for i, tr in filter(lambda tup: tup[0] > 0, enumerate(soup.find('table', {'width': '99%'}))):
                if tr.contents[1].string in ['魔法', 'Magic', 'БР']:
                    if tr.contents[2].a:
                        if tr.contents[3].string in ['所有人', 'Everyone', 'Для всех', *(
                                [user_name] if MAGIC_SELF else [])]:
                            tid = int(tr.contents[2].a['href'][15:])
                            magic_id = int(tr.contents[0].string)
                            if magic_id not in self.checked:
                                if self.magic_id_0 and magic_id == self.magic_id_0:  # 新增魔法数量超过了 deque 最大长度
                                    all_checked = True
                                    break
                                if self.first_time and not RUN_CRONTAB and not DOWNLOAD_ON_FIRST_TIME:
                                    self.checked.append(magic_id)
                                else:
                                    yield magic_id, tid
                            else:
                                all_checked = True
                                break
            if all_checked:
                break
            else:
                index += 1  # 新增魔法数量不小于单页魔法数量
        if self.first_time and not RUN_CRONTAB:
            with open(f'{DATA_PATH}', 'w', encoding='utf-8') as f:
                f.write(f'checked = {self.checked}\n')
        self.first_time = False
        self.magic_id_0 = max(self.checked)

    @staticmethod
    def dl_to(to_name, dl_link):
        tid = dl_link.split('&passkey')[0].split('id=')[1]
        if f'[U2].{tid}.torrent' in os.listdir(BK_DIR):
            if not RE_DOWNLOAD:
                logger.info(f'Torrent {tid} | you have downloaded this torrent before, passed')
                return
        else:
            with open(f'{BK_DIR}/[U2].{tid}.torrent', 'wb') as f:
                f.write(get(dl_link, **R_ARGS).content)
        shutil.copy(f'{BK_DIR}/[U2].{tid}.torrent', f'{WT_DIR}/[U2].{tid}.torrent')
        tid = dl_link.split('&passkey')[0].split('id=')[1]
        logger.info(f'Download torrent {tid}, name {to_name}')

    @classmethod
    def get_tz(cls, soup):
        tz_info = soup.find('a', {'href': 'usercp.php?action=tracker#timezone'})['title']
        tz = [tz_info[len(pre):-len(suf)].strip() for pre, suf in cls.pre_suf if tz_info.startswith(pre)][0]
        return pytz.timezone(tz)

    @staticmethod
    def timedelta(date, timezone):
        dt = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')
        return time() - timezone.localize(dt).timestamp()

    @staticmethod
    def get_pro(td):
        pro = {'ur': 1.0, 'dr': 1.0}
        pro_dict = {'free': {'dr': 0.0}, '2up': {'ur': 2.0}, '50pct': {'dr': 0.5}, '30pct': {'dr': 0.3}, 'custom': {}}
        for img in td.select('img') or []:
            if not [pro.update(data) for key, data in pro_dict.items() if key in img['class'][0]]:
                pro[{'arrowup': 'ur', 'arrowdown': 'dr'}[img['class'][0]]] = float(img.next.text[:-1].replace(',', '.'))
        return list(pro.values())

    @staticmethod
    def get_soup(url):
        magic_page = get(url, **R_ARGS).text
        if url != 'https://u2.dmhy.org/promotion.php?action=list&page=0':
            logger.debug(f'Download page: {url}')
        return BeautifulSoup(magic_page.replace('\n', ''), 'lxml')

    def analyze_magic(self, magic_id, tid):
        soup = self.get_soup(f'https://u2.dmhy.org/details.php?id={tid}')
        self.checked.append(magic_id)
        with open(f'{DATA_PATH}', 'w', encoding='utf-8') as f:
            f.write(f'checked = {self.checked}\n')
        delta = self.timedelta(soup.time.get('title') or soup.time.text, self.get_tz(soup))

        if delta < MIN_DAY * 86400:
            logger.debug(f'Torrent {tid} | time < {MIN_DAY} days')
            return

        aa = soup.select('a.index')
        to_name = aa[0].text[5:-8]
        dl_link = f"https://u2.dmhy.org/{aa[1]['href']}"
        magic_page_soup = None

        if not DOWNLOAD_NON_FREE:
            if [self.get_pro(tr.contents[1])[1] for tr in soup.find('table', {'width': '90%'})
                    if tr.td.text in ['流量优惠', '流量優惠', 'Promotion', 'Тип раздачи (Бонусы)']][0] > 0:
                logger.debug(f'torrent {tid} | is not free, will pass if no free magic in delay.')
                magic_page_soup = self.get_soup(f'https://u2.dmhy.org/promotion.php?action=detail&id={magic_id}')
                tbody = magic_page_soup.find('table', {'width': '75%', 'cellpadding': 4}).tbody
                if self.get_pro(tbody.contents[6].contents[1])[1] == 0:
                    delay = -self.timedelta(tbody.contents[4].contents[1].string, self.get_tz(magic_page_soup))
                    if -1 < delay < 60:
                        logger.debug(f'Torrent {tid} | free magic {magic_id} will be effective in {int(delay)}s')
                    else:
                        return
                else:
                    return

        seeder_count = int(re.search(r'(\d+)', soup.find('div', {'id': 'peercount'}).b.text).group(1))
        if seeder_count > 0:
            if seeder_count <= MAX_SEEDER_NUM:
                self.dl_to(to_name, dl_link)
            else:
                if not magic_page_soup:
                    magic_page_soup = self.get_soup(f'https://u2.dmhy.org/promotion.php?action=detail&id={magic_id}')
                comment = magic_page_soup.legend.parent.contents[1].text
                if '搭' in comment and '桥' in comment or '加' in comment and '速' in comment:
                    user = magic_page_soup.select('table.main bdo')[0].text
                    logger.debug(f'Torrent {tid} | user {user} is looking for help, downloading...')
                    self.dl_to(to_name, dl_link)
                else:
                    logger.debug(f'Torrent {tid} | seeders > {MAX_SEEDER_NUM}')
        else:
            logger.debug(f'Torrent {tid} | no seeders, passed.')

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
