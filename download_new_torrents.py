# python 版本 3.6 及以上，依赖: pip3 install requests bs4 lxml loguru pymongo
# 自己用了一下还可行，虽然我是直接加到 deluge...

import os
import re
from collections import deque

import pytz

from datetime import datetime as dt
from functools import wraps
from time import sleep, time
from bs4 import BeautifulSoup
from loguru import logger
from requests import get

# *************************必填配置************************
cookies = {'nexusphp_u2': ''}
passkey = ''

# ************************可修改配置***********************
save_path = 'C:/Downloads/torrents'  # 下载文件夹，可以用 bt 客户端监控
proxies = {  # 代理
    # 'http': 'http://127.0.0.1:10809', 'https': 'http://127.0.0.1:10809'
}
headers = {
    'authority': 'u2.dmhy.org',
    'accept-encoding': 'gzip, deflate',
    'accept-language': 'zh-CN,zh;q=0.8',
    'referer': 'https://u2.dmhy.org/index.php',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/98.0.4758.102 Safari/537.36 Edg/98.0.1108.62'
}
interval = 300  # 爬网页的间隔
mgdb = False  # 将种子数据保存到 mongodb，需要安装 mongodb 数据库
download_sticky = True  # 是否下载顶置
download_no_seeder_sticky = True  # 是否下载无人做种的顶置，为 True 时还是不会下载平均进度为 0 的种子
download_no_free_sticky = True  # 是否下载不是 free 的顶置种子
download_no_free_non_sticky = False  # 是否下载不是 free 的非顶置种子
eval_all_keys = False  # 获取种子所有信息，不开的话用到哪个就获取哪个

# *************************日志设置************************
log_path = f'{os.path.splitext(__file__)[0]}.log'
logger.add(level='DEBUG', rotation='2 MB', sink=log_path)

# ***********************程序保存数据**********************
data_path = f'{os.path.splitext(__file__)[0]}.data.txt'  # 数据保存文件
checked = deque([], maxlen=300)
'''如果某个种子的 id 在 checked 里，那么该种子详细页不会爬第二次，
如果去获取只有详细页才有的信息，比如 info_hash 值，会返回 None'''
added = deque([], maxlen=300)
'''如果某个种子的 id 在 added 里，那么筛选种子的时候会跳过'''


# *************************END****************************


def write_list(name):
    if name in globals():
        with open(data_path, 'r', encoding='utf-8') as f1, open(f'{data_path}.bak', 'w', encoding='utf-8') as f2:
            k = 0
            for _line in f1:
                if _line.startswith(name):
                    k = 1
                    f2.write(f'{name} = {globals()[name]}\n')
                else:
                    f2.write(_line)
            if k == 0:
                f2.write(f'{name} = {globals()[name]}\n')
        os.remove(data_path)
        os.rename(f'{data_path}.bak', data_path)


def get_url(url):
    try:
        html = get(url, cookies=cookies, headers=headers, proxies=proxies)
        if html.status_code < 400:
            if url != 'https://u2.dmhy.org/torrents.php':
                logger.info(f'download page {url}')
            return html.text
    except Exception as e:
        logger.error(e)


detail_key_dict = {
        'filename': ['下载', '下載', 'Download', 'Скачивание'],
        'author': ['发布人', '發佈人', '發布人', 'Uploader', 'Загрузил'],
        'hash': ['种子信息', '種子訊息', 'Torrent Info', 'Информация о торренте'],
        'description': ['描述', '描述', 'Description', 'Описание'],
        'progress': ['活力度', 'Health', 'Целостность'],
        'geoips': ['同伴', 'Peers', 'Всего Участников']
    }


class U2Web:
    def __init__(self):
        self.keys = [key[1:] for key, obj in type(self).__dict__.items()
                     if isinstance(obj, property) and key.startswith('_')]
        self.info = {}
        self.tr = None
        self.tr1 = None
        self.trs = None
        self.d_url = None
        self.t_url = None
        self.table1 = []
        self.tz = ''
        self.passkey = passkey

    def __getattr__(self, item):
        if item in self.keys:
            return getattr(self, f'_{item}')
        else:
            raise KeyError(f'Key {item} is not supported. These are all supported keys: {self.keys}')

    def torrent_page(self):
        page = get_url('https://u2.dmhy.org/torrents.php')
        soup = BeautifulSoup(page.replace('\n', ''), 'lxml')
        tz_info = soup.find('a', {'href': 'usercp.php?action=tracker#timezone'})['title']
        pre_suf = [['时区', '，点击修改。'], ['時區', '，點擊修改。'], ['Current timezone is ', ', click to change.']]
        self.tz = [tz_info[len(pre):][:-len(suf)].strip() for pre, suf in pre_suf if tz_info.startswith(pre)][0]

        table = soup.select('table.torrents')[0]
        for self.tr in table.contents[1:]:
            self.info = {}
            if self.tid not in added:  # 过滤已经添加的种子
                self.trs = str(self.tr)
                yield self.tr

    def _seeding(self):  # 是否正在做种
        return bool('seedhlc_current' in self.trs)

    def _leeching(self):  # 是否正在下载
        return bool('leechhlc_current' in self.trs)

    def _sticky(self):  # 是否顶置
        return bool('sticky' in self.trs)

    '''
    def _hot(self):  # 是否热门
        return bool(self.tr.select('span.hot'))
    '''

    def _incomplete(self):  # 是否曾经未完成
        return bool('incomplete' in self.trs)

    def _completed(self):  # 是否曾经完成
        return bool('snatchhlc_finish' in self.trs)

    def _auxseed(self):  # 是否曾经辅种
        return bool('snatchhlc_auxseed' in self.trs)

    def _tid(self):  # 种子 id
        return int(self.tr.contents[1].a['href'][15:-6])

    def _title(self):  # 标题
        return self.tr.contents[1].a.text

    def _small_descripton(self):  # 副标题
        tooltip = self.tr.find('span', {'class': 'tooltip'})
        return tooltip.text if tooltip else None

    def _seeder_num(self):  # 做种数
        return int(self.tr.contents[5].string)

    def _leecher_num(self):  # 下载数
        return int(self.tr.contents[6].contents[0].string)

    def _completes(self):  # 完成数
        return int(self.tr.contents[7].string)

    def _date(self):  # 发布日期(字符串)
        return self.tr.contents[3].time.get('title') or self.tr.contents[3].time.get_text(' ')

    def _size(self):  # 体积(字符串)
        return self.tr.contents[4].get_text(' ')

    def _promotion(self):  # 上传下载比率
        pro = {'ur': 1.0, 'dr': 1.0}
        pro_dic = {'free': {'dr': 0.0}, 'twoup': {'ur': 2.0}, 'halfdown': {'dr': 0.5}, 'thirtypercent': {'dr': 0.3}}
        if self.tr.get('class'):
            [pro.update(data) for key, data in pro_dic.items() if key in self.tr['class'][0]]
        td = self.tr.tr and self.tr.select('tr')[1].td or self.tr.select('td')[1]
        pro_dic_1 = {'free': {'dr': 0.0}, '2up': {'ur': 2.0}, '50pct': {'dr': 0.5}, '30pct': {'dr': 0.3}, 'custom': {}}
        for img in td.select('img') or []:
            if not [pro.update(data) for key, data in pro_dic_1.items() if key in img['class'][0]]:
                pro[{'arrowup': 'ur', 'arrowdown': 'dr'}[img['class'][0]]] = float(img.next.text[:-1].replace(',', '.'))
        for span in td.select('span') or []:
            [pro.update(data) for key, data in pro_dic.items() if key in (span.get('class') and span['class'][0] or '')]
        return list(pro.values())

    def _torrentsign(self):  # 种子签名
        if 'torrentsign' in self.trs:
            return self.tr.select('span.torrentsign')[0].text

    def _pro_end_date(self):  # 优惠结束时间
        if self.tr.contents[1].time:
            return self.tr.contents[1].time.get('title') or self.tr.contents[1].time.text

    def _ani_link(self):  # anidb 链接
        td = self.tr.select('tr')[1].contents[1]
        if td.string:
            return td.a['href']

    def _rating(self):  # anidb 评分
        num = self.tr.select('tr')[1].contents[1].string
        if num not in (None, ' - '):
            return float(num)

    def detail_page(self):  # 详情页很多地方结构的不固定，可能用正则表达式可能会好点？
        if self.tid not in checked:
            self.d_url = self.t_url
            soup = BeautifulSoup(get_url(self.d_url).replace('\n', ''), 'lxml')
            self.passkey = soup.select('a.index')[1]['href'].split('&passkey=')[1][:-8]
            self.table1 = soup.find('table', {'width': '90%'})
            checked.append(self.info['tid'])
            write_list('checked')
        for self.tr1 in self.table1:
            yield self.tr1

    def _filename(self):  # 种子内容文件名
        return self.tr1.a.text[5:-8]

    def _author(self):  # 发布者 uid
        if not any(a in str(self.tr1) for a in ['匿名', 'torrentsign', 'Anonymous', 'Анонимно']):
            return self.tr1.s and self.tr1.s.text or self.tr1.a['href'][19:]

    '''
    def _descrption(self):  # 详细描述
        return self.tr1.bdo.text
    '''

    def _hash(self):  # info_hash
        return self.tr1.tr.contents[-2].contents[1].strip()

    def _progress(self):  # (包括做种者在内的) 平均进度
        if not any(st in str(self.tr1) for st in ['没有流量', '沒有流量', 'No Traffic', 'Не зафиксировано']):
            return int(self.tr1.b.previous_element.strip()[1:-2])

    def _geoips(self):  # 做种者的地理位置信息
        if int(re.search(r'(\d+)', self.tr1.b.text).group(1)) > 0:
            peerlist = get_url(f'https://u2.dmhy.org/viewpeerlist.php?id={self.tid}')
            table = BeautifulSoup(peerlist.replace('\n', ' '), 'lxml').table
            ips = []
            for tr in filter(lambda _tr: 'nowrap' in str(_tr), table):
                ip = {}
                for i, span in enumerate(tr.contents[0]):
                    if i == 0:
                        ip['user'] = tr.i and str(tr.i) or tr.bdo.text  # 用户名
                    else:
                        ip[span['class'][1]] = span['title']
                ips.append(ip)
            return ips

    @property
    def secs(self):  # 发布时间到现在的间隔(s)，不是 property
        tm = dt.strptime(self.date, '%Y-%m-%d %H:%M:%S')
        return int(time() - pytz.timezone(self.tz).localize(tm).timestamp())

    @property
    def gbs(self):  # 种子体积(gb)，不是 property
        [num, unit] = self.size.split(' ')
        _pow = ['MiB', 'GiB', 'TiB', '喵', '寄', '烫', 'MiБ', 'GiБ', 'TiБ'].index(unit) % 3
        return float(num.replace(',', '.')) * 1024 ** (_pow - 1)

    def select_torrent(self):
        """
        选择种子，符合条件返回 True。有些值可能为空
        规则自己写吧，反正应该很好懂，看这个逻辑也不是很方便能用配置描述....
        """
        if not self.seeding and not self.leeching:  # 过滤下载中和做种中的种子
            if self.sticky:  # 顶置种子
                if download_sticky:
                    if not download_no_free_sticky and self.promotion[1] > 0:
                        return
                    if self.seeder_num > 0:  # 做种数大于 0 ，直接下载
                        return True
                    if download_no_seeder_sticky:
                        return
                    if self.leecher_num > 5:
                        if self.progress is not None and self.progress > 0:
                            # 做种数小于于 0 ，检查下载者进度，如果平均进度全为 0 不下载
                            return True
            else:
                if not download_no_free_non_sticky and self.promotion[1] > 0:
                    return
                if self.secs < 2 * interval:  # 发布不久，直接下载
                    return True
                if self.seeder_num < 10 or self.leecher_num > 20:  # 根据做种数和下载数判断是否要下载
                    if self.progress is not None and self.progress < 30:  # 检查平均进度
                        return True

    def rss(self):
        while True:
            try:
                for self.tr in self.torrent_page():
                    if self.select_torrent():
                        torrent = f'{save_path}/[U2].{self.tid}.torrent'
                        link = f'https://u2.dmhy.org/download.php?id={self.tid}&passkey={self.passkey}&https=1'
                        with open(torrent, 'wb') as to:
                            content = get(link, headers=headers, proxies=proxies).content
                            to.write(content)
                        added.append(self.tid)
                        write_list('added')
                        logger.info(f'add torrent {self.tid}')
                        '''  # 直接添加到 deluge
                        from deluge_client import LocalDelugeRPCClient
                        from base64 import b64encode
                        client = LocalDelugeRPCClient('127.0.0.1', 58846, '', '')
                        client.reconnect()
                        client.core.add_torrent_file(
                            f'[U2].{self.tid}.torrent', b64encode(content), {'add_paused': False})
                        '''
                        if eval_all_keys:  # 获取种子所有信息
                            for _key in self.keys:
                                getattr(self, _key)
                        for key in list(self.info.keys()):  # 删掉空的键
                            if not self.info[key]:
                                del self.info[key]
                        if mgdb:
                            col.insert_one(self.info)
                        else:
                            logger.debug(f'-----------  torrent info  ----------\n{self.info}')
            except Exception as e:
                logger.exception(e)
            finally:
                sleep(interval)

    def value(func):
        @property
        @wraps(func)
        def wrapper(self, *args, **kw):
            name = func.__name__[1:]
            if name not in self.info:  # sel.info 中没有这个 key，说明之前没有获取
                if name in detail_key_dict:  # key 只有详细页才有
                    self.t_url = f'https://u2.dmhy.org/details.php?id={self.tid}&hit=1'
                    if self.tid in checked and self.d_url != self.t_url:
                        # 已经检查过一次，并且详情页不在内存中，返回 None
                        self.info[name] = None
                    else:
                        for self.tr1 in self.detail_page():
                            if any(word in self.tr1.td.text for word in detail_key_dict[name]):
                                self.info[name] = func(self, *args, **kw)
                                break
                            else:
                                self.info[name] = None
                else:
                    self.info[name] = func(self, *args, **kw)
            return self.info.get(name)
        return wrapper

    for name in list(vars()):
        obj = vars()[name]
        if hasattr(obj, '__get__') and not hasattr(obj, '__set__'):
            if name.startswith('_') and not (name.startswith('__') and name.endswith('__')):
                vars()[name] = value(obj)

    del value, name, obj


if __name__ == '__main__':
    if mgdb:
        import pymongo
        dbclient = pymongo.MongoClient('mongodb://localhost:27017/')
        base = dbclient['U2']
        col = base['torrent_info']
        torrent_info = col.find().sort('_id', -1).limit(50)
        added.extend([info['tid'] for info in torrent_info])
        write_list('added')

    with open(data_path, 'a', encoding='utf-8'):
        pass
    with open(data_path, 'r', encoding='utf-8') as f:
        for line in f:
            if any(line.startswith(var) for var in ['added', 'checked']):
                exec(line)

    u2 = U2Web()
    u2.rss()
