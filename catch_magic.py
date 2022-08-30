"""必须填写 cookie，之后修改 BK_DIR 和 WT_DIR，即可运行"""

import gc
import json
import os
import re
import shutil
import pytz

from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from time import sleep, time

from requests import get, ReadTimeout
from bs4 import BeautifulSoup
from loguru import logger

RUN_CRONTAB = False  # 如果为真，代表脚本不会死循环，运行一次脚本退出，需要以一定间隔运行脚本，主要解决内存问题；否则一直循环运行不退出。
RUN_TIMES = 1  # RUN_CRONTAB 为真时运行脚本一次 run 函数循环的次数，默认运行一次脚本结束，但如果频繁运行影响性能的话可以改大
R_ARGS = {'headers': {'cookie': 'nexusphp_u2=',  # 填网站 cookie，不要空格
                      'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.81 Safari/537.36'
                      },
          'timeout': 20,  # 超时秒数
          'proxies': {  # 'http': "127.0.0.1:10809", 'https': "127.0.0.1:10809"  # 代理
          }
          }  # requests 模块参数
BK_DIR = '/root/backup'  # 备份种子文件夹路径
WT_DIR = '/de/wt'  # BT 客户端监控文件夹
INTERVAL = 120  # 检查魔法的时间间隔
MAX_SEEDER_NUM = 5  # 最大的做种人数，超过不下载
LOG_PATH = f'{os.path.splitext(__file__)[0]}.log'  # 日志文件路径
DATA_PATH = f'{os.path.splitext(__file__)[0]}.data.txt'  # 数据文件路径
DOWNLOAD_NON_FREE = False  # 如果为真为下载不是 free 的种子，否则的话只下载 free 的种子
MIN_DAY = 7  # 种子发布时间超过此天数判断为旧种子，否则判断为新种子
DOWNLOAD_OLD = True  # 是否下载旧种子
DOWNLOAD_NEW = False  # 是否下载新种子
MAGIC_SELF = False  # 如果为真，会下载给自己放魔法的种子，否则不下载
EFFECTIVE_DELAY = 60  # 如果该魔法是 free 并且生效时间在此之内，就算种子不是 free 也直接下载
DOWNLOAD_DEAD_TO = False  # 默认不下载无人做种的旧种子(新种总有人做种，所以不考虑有没有人做种一律下载)，如果要下载改成 True
RE_DOWNLOAD = True  # 如果为 False，检测到备份文件夹有该种子则不再次下载
CHECK_PEERLIST = False  # 检查 peer 列表，如果已经在做种或者在下载则不下载种子
DA_QIAO = True  # 是否搭桥，如果搭桥，即使做种人数超过最大值魔法咒语有’搭桥‘或’加速‘也会下载
MIN_RE_DL_DAYS = 0  # 离最近一次下载该种子的最小天数，小于这个天数不下载种子
CAT_FILTER = []  # 种子类型为其中之一则下载，类型见 torrents.php，多个用逗号隔开，不填就不进行类型过滤，比如 ['BDMV', 'Lossless Music']
SIZE_FILTER = [0, -1]  # 体积过滤，第一个数为体积最小值(GB)，第二个为最大值(GB)，-1 表示不设上限
NAME_FILTER = []  # 过滤种子标题，如果标题或者文件名中包含这些字符串之一则排除不下载，多个用逗号隔开，字符串要加引号，比如 ['BDrip']


class CatchMagic:
    pre_suf = [['时区', '，点击修改。'], ['時區', '，點擊修改。'], ['Current timezone is ', ', click to change.']]

    def __init__(self):
        logger.add(level='DEBUG', sink=LOG_PATH, rotation='2 MB')
        self.checked, self.magic_id_0 = deque([], maxlen=200), None
        with open(DATA_PATH, 'a', encoding='utf-8'):
            pass
        with open(DATA_PATH, 'r', encoding='utf-8') as fp:
            try:
                data = json.load(fp)
                self.checked = deque(data['checked'], maxlen=200)
                self.magic_id_0 = data['id_0']
            except json.JSONDecodeError:
                pass
        self.first_time = True

    def all_effective_magic(self):
        all_checked = True if self.first_time and not self.magic_id_0 else False
        index = 0
        id_0 = self.magic_id_0

        while True:
            soup = self.get_soup(f'https://u2.dmhy.org/promotion.php?action=list&page={index}')
            user_id = soup.find('table', {'id': 'info_block'}).a['href'][19:]
            for i, tr in filter(lambda tup: tup[0] > 0, enumerate(soup.find('table', {'width': '99%'}))):
                magic_id = int(tr.contents[0].string)
                if index == 0 and i == 1:
                    self.magic_id_0 = magic_id
                    if self.first_time and id_0 and magic_id - id_0 > 10 * INTERVAL:
                        all_checked = True
                if tr.contents[5].string in ['Expired', '已失效']:
                    all_checked = True
                    break
                if tr.contents[1].string in ['魔法', 'Magic', 'БР']:
                    if not tr.contents[3].a and tr.contents[3].string in ['所有人', 'Everyone', 'Для всех'] or \
                            MAGIC_SELF and tr.contents[3].a and tr.contents[3].a['href'][19:] == user_id:
                        if tr.contents[5].string not in ['Terminated', '终止', '終止', 'Прекращён']:
                            if tr.contents[2].a:
                                tid = int(tr.contents[2].a['href'][15:])
                                if magic_id not in self.checked and magic_id != id_0:
                                    if self.first_time and all_checked:
                                        self.checked.append(magic_id)
                                    else:
                                        yield magic_id, tid
                                    continue

                if magic_id == id_0:
                    all_checked = True
                    break
                elif magic_id not in self.checked:
                    self.checked.append(magic_id)

            if all_checked:
                break
            else:
                index += 1  # 新增魔法数量不小于单页魔法数量

        if self.magic_id_0 != id_0:
            with open(f'{DATA_PATH}', 'w', encoding='utf-8') as fp:
                json.dump({'checked': list(self.checked), 'id_0': self.magic_id_0}, fp)
        self.first_time = False

    def dl_to(self, to_info):
        tid = to_info['dl_link'].split('&passkey')[0].split('id=')[1]

        if CHECK_PEERLIST and to_info['last_dl_time']:
            peer_list = self.get_soup(f'https://u2.dmhy.org/viewpeerlist.php?id={tid}')
            tables = peer_list.find_all('table')
            for table in tables or []:
                for tr in filter(lambda _tr: 'nowrap' in str(_tr), table):
                    if tr.get('bgcolor'):
                        logger.info(f"Torrent {tid} | you are already "
                                    f"{'downloading' if len(tr.contents) == 12 else 'seeding'} the torrent")
                        return

        if f'[U2].{tid}.torrent' in os.listdir(BK_DIR):
            if not RE_DOWNLOAD:
                logger.info(f'Torrent {tid} | you have downloaded this torrent before')
                return
        else:
            with open(f'{BK_DIR}/[U2].{tid}.torrent', 'wb') as f:
                f.write(get(to_info['dl_link'], **R_ARGS).content)

        shutil.copy(f'{BK_DIR}/[U2].{tid}.torrent', f'{WT_DIR}/[U2].{tid}.torrent')
        logger.info(f"Download torrent {tid}, name {to_info['to_name']}")

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
        aa = soup.select('a.index')
        to_info = {'to_name': aa[0].text[5:-8], 'dl_link': f"https://u2.dmhy.org/{aa[1]['href']}"}

        if NAME_FILTER:
            title = soup.find('h1', {'align': 'center', 'id': 'top'}).text
            if any(st in title or st in to_info['to_name'] for st in NAME_FILTER):
                logger.debug(f'Torrent {tid} | torrent excluded by NAME_FILTER')
                return

        if CAT_FILTER:
            cat = soup.time.parent.contents[7].strip()
            if cat not in CAT_FILTER:
                logger.debug(f'Torrent {tid} | torrent category {cat} does not match, passed')
                return

        if SIZE_FILTER and not (SIZE_FILTER[0] <= 0 and SIZE_FILTER[1] == -1):
            size_str = soup.time.parent.contents[5].strip().replace(',', '.').replace('Б', 'B')
            [num, unit] = size_str.split(' ')
            _pow = ['MiB', 'GiB', 'TiB', '喵', '寄', '烫'].index(unit) % 3
            gb = float(num) * 1024 ** (_pow - 1)
            if gb < SIZE_FILTER[0] or SIZE_FILTER[1] != -1 and gb > SIZE_FILTER[1]:
                logger.debug(f'Torrent {tid} | torrent size {size_str} does not match, passed')
                return

        if CHECK_PEERLIST or MIN_RE_DL_DAYS > 0:
            for tr in soup.find('table', {'width': '90%'}):
                if tr.td.text in ['My private torrent', '私人种子文件', '私人種子文件', 'Ваш личный торрент']:
                    time_str = tr.find_all('time')
                    if not time_str:
                        to_info['last_dl_time'] = None
                    else:
                        date = time_str[1].get('title') or time_str[1].text
                        to_info['last_dl_time'] = time() - self.timedelta(date, self.get_tz(soup))
            if MIN_RE_DL_DAYS > 0 and to_info['last_dl_time']:
                if time() - to_info['last_dl_time'] < 86400 * MIN_RE_DL_DAYS:
                    logger.debug(f"Torrent {tid} | You have downloaded this torrent "
                                 f"{(time() - to_info['last_dl_time']) // 86400} days before, passed")
                    return

        delta = self.timedelta(soup.time.get('title') or soup.time.text, self.get_tz(soup))
        seeder_count = int(re.search(r'(\d+)', soup.find('div', {'id': 'peercount'}).b.text).group(1))
        magic_page_soup = None

        if delta < MIN_DAY * 86400:
            if DOWNLOAD_NEW:
                if seeder_count > MAX_SEEDER_NUM:
                    logger.debug(f'Torrent {tid} | seeders > {MAX_SEEDER_NUM}, passed')
                else:
                    self.dl_to(to_info)
            else:
                logger.debug(f'Torrent {tid} | time < {MIN_DAY} days, passed')
            return
        elif not DOWNLOAD_OLD:
            logger.debug(f'Torrent {tid} | time > {MIN_DAY} days, passed')
            return

        if not DOWNLOAD_NON_FREE:
            if [self.get_pro(tr.contents[1])[1] for tr in soup.find('table', {'width': '90%'})
                    if tr.td.text in ['流量优惠', '流量優惠', 'Promotion', 'Тип раздачи (Бонусы)']][0] > 0:
                logger.debug(f'torrent {tid} | is not free, will pass if no free magic in delay.')
                magic_page_soup = self.get_soup(f'https://u2.dmhy.org/promotion.php?action=detail&id={magic_id}')
                tbody = magic_page_soup.find('table', {'width': '75%', 'cellpadding': 4}).tbody
                if self.get_pro(tbody.contents[6].contents[1])[1] == 0:
                    time_tag = tbody.contents[4].contents[1].time
                    delay = -self.timedelta(time_tag.get('title') or time_tag.text, self.get_tz(magic_page_soup))
                    if -1 < delay < EFFECTIVE_DELAY:
                        logger.debug(f'Torrent {tid} | free magic {magic_id} will be effective in {int(delay)}s')
                    else:
                        return
                else:
                    return

        if seeder_count > 0 or seeder_count == 0 and DOWNLOAD_DEAD_TO:
            if seeder_count <= MAX_SEEDER_NUM:
                self.dl_to(to_info)
                return
            elif DA_QIAO:
                if not magic_page_soup:
                    magic_page_soup = self.get_soup(f'https://u2.dmhy.org/promotion.php?action=detail&id={magic_id}')
                comment = magic_page_soup.legend.parent.contents[1].text
                if '搭' in comment and '桥' in comment or '加' in comment and '速' in comment:
                    user = magic_page_soup.select('table.main bdo')[0].text
                    logger.info(f'Torrent {tid} | user {user} is looking for help, downloading...')
                    self.dl_to(to_info)
                    return
            logger.debug(f'Torrent {tid} | seeders > {MAX_SEEDER_NUM}, passed')
        else:
            logger.debug(f'Torrent {tid} | no seeders, passed')

    def run(self):
        id_0 = self.magic_id_0
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(self.analyze_magic, magic_id, tid): magic_id
                       for magic_id, tid in self.all_effective_magic()}
            if futures:
                error = False
                for future in as_completed(futures):
                    try:
                        future.result()
                        self.checked.append(futures[future])
                    except Exception as er:
                        error = True
                        if isinstance(er, ReadTimeout):
                            logger.error(er)
                        else:
                            logger.exception(er)
                if error:
                    self.magic_id_0 = id_0
                with open(f'{DATA_PATH}', 'w', encoding='utf-8') as fp:
                    json.dump({'checked': list(self.checked), 'id_0': self.magic_id_0}, fp)


@logger.catch()
def main(catch):
    for _ in range(RUN_TIMES):
        try:
            catch.run()
        except ReadTimeout as e:
            logger.error(e)
        finally:
            if _ != RUN_TIMES - 1 or not RUN_CRONTAB:
                gc.collect()
                sleep(INTERVAL)


c = CatchMagic()
if RUN_CRONTAB:
    main(c)
else:
    while True:
        main(c)
