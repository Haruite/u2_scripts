"""发糖脚本，用于在论坛或者种子评论区发糖，可以随时停止和重新运行
解析回复内容会自动去掉引用、代码、链接，但不会去掉折叠内容
不想吐槽这个代码，明明就是这么简单的功能因为抠逻辑写得难读得屎一样"""

import json
import os
import random
import re
from time import sleep
from inspect import isfunction
from typing import Union, Dict, List, Tuple, Callable, Any

import bs4.element
import requests

from loguru import logger
from bs4 import BeautifulSoup

URL = ''  # type: str
'''帖子、种子、候选的 url，直接复制即可'''
COOKIES = {'nexusphp_u2': ''}  # type: Dict[str, str]
'''网站 cookie'''
PROXIES = {'http': '', 'https': ''}  # type: Union[Dict[str, Union[str, None]], None]
'''代理'''
UC = 50000  # type: Union[int, Tuple[int, int], Tuple[int, int, Union[int, float]], Tuple[Union[int, float], Union[int, float], Callable], List[Union[int, float, Callable]]]
'''设定发糖数量，有四种方法
第一种，设定为一个固定值，例 
UC = 50000

第二种，设定一个最小值和最大值，例 
UC = 50000, 150000 
程序会自动从两个值之间取随机数，随机数为均匀分布，理论上平均值期望就是两者平均数

第三种，设定一个最小值、平均值期望和最大值例，例
UC = 20000, 100000, 660000
脚本会使用幂函数来实现，公式是 最小值+x^((最大值-平均值期望)/(平均值期望-最小值))
x 为一个随机数，最小为 0，最大让函数值达到 UC 设定的最大值
最大值可以很大，但是越大意味着收到最小金额的概率越大

第四种，设定一个随机数区间和一个函数，例
UC = 0, 2**(6/7), lambda x: int(round(10000*(2 + x ** 7)))
第一个数是区间下限，第二个是区间上限，第三个是函数，
脚本将使用随机数区间生成的随机数作为函数的参数，返回值作为发糖金额
'''
NUM = -1  # type: int
'''发糖人数， -1 表示不限制'''
TEXT = True  # type: Any
'''是否解析回帖内容，如果不解析一律发给回复者本人，否则的话如果发给回复中解析出有效的用户 id (没有还是发给本人)'''
RGX = r'((?<![\d.])\d{3,}(?![\d.]))'  # type: Union[str, re.Pattern]
'''从回复内容中解析发糖 uid 的正则表达式，匹配三位及以上整数（排除小数）'''
RE = 1  # type: int
'''同一个用户最大转账次数(一条评论算一次)，-1 为不限制'''
EXT = True  # type: Any
'''为真时 uc 不足直接退出脚本，否则等到 uc 恢复继续发糖'''
MSG = ''  # type: str
'''留言'''
INFO = False  # type: Any
'''是否在留言中注明帖子和评论 id 等信息'''
UPDATE = False  # type: Any
'''为真时每次给一个人发糖前都会检查帖子内容，否则等所有人发完了再检查帖子内容'''
DATA_PATH = f'{os.path.splitext(__file__)[0]}.info'  # type: str
'''数据保存路径'''
LOG_PATH = f'{os.path.splitext(__file__)[0]}.log'  # type: str
'''日志文件路径'''
R_ARGS = {'cookies': COOKIES,
          'headers': {'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
                                    'Chrome/103.0.5060.134 Safari/537.36 Edg/103.0.1264.77'},
          'proxies': PROXIES,
          'timeout': 20,
          'verify': True}
'''requests 模块参数'''


class TransferUCoin:
    def __init__(self):
        self.total_info = {}
        with open(DATA_PATH, 'a'):
            pass
        with open(DATA_PATH, 'r') as fp:
            try:
                self.total_info = json.load(fp)
            except json.JSONDecodeError:
                pass

        if URL.split('u2.dmhy.org/')[1].startswith('forum'):
            topic_id = re.findall(r'topicid=(\d+)', URL)[0]
            self.url = f'https://u2.dmhy.org/forums.php?action=viewtopic&topicid={topic_id}'
            self.page_info = f'topic_id {topic_id}'
        else:
            torrent_id = re.findall(r'id=(\d+)', URL)[0]
            if 'offers' in URL:
                self.url = f'https://u2.dmhy.org/offers.php?id={torrent_id}&off_details=1'
            elif 'details' in URL:
                self.url = f'https://u2.dmhy.org/details.php?id={torrent_id}&cmtpage=1'
            self.page_info = f'torrent_id {torrent_id}'

        if self.page_info not in self.total_info:
            self.total_info[self.page_info] = {}
        self.info = self.total_info[self.page_info]
        self.uid = None
        self.uc_amount = None
        self.index = 0
        self.id_info = None
        self.transfer_num = 0

    def get_soup(self, url):
        for _ in range(5):
            try:
                html = requests.get(url, **R_ARGS)
                logger.debug(f'下载网页 | {url}')
                if html.status_code < 400:
                    soup = BeautifulSoup(html.text.replace('\n', ''), 'lxml')
                    info_block = soup.find('table', {'id': 'info_block'})
                    self.uc_amount = sum(map(
                        lambda x: int(x[0].text) * x[1],
                        zip(info_block.find_all('span', {'class': re.compile('^ucoin-symbol')}), (10000, 100, 1))
                    ))
                    if not self.uid:
                        self.uid = int(info_block.a['href'][19:])
                    return soup
            except Exception as er:
                logger.error(er)

    def save(self):
        with open(DATA_PATH, 'w', encoding='utf-8') as fp:
            json.dump(self.total_info, fp)

    def run(self):
        while True:
            if NUM != -1 and self.transfer_num >= NUM:
                logger.info(f'转账人数已达到设定值 {NUM}，退出程序')
                exit()

            self.parse_page()
            _list = list(self.info.keys())
            index = (-1 if not self.id_info else _list.index(self.id_info)) + 1
            i = 0

            if len(_list) > index:
                for id_info in _list[index:]:
                    info = self.info[id_info]
                    if info['post_uid'] not in [self.uid, None]:
                        if info['transferred'] == 0 or 'expect_uc' in info and info['transferred'] < info['expect_uc']:
                            if i > 0 and UPDATE:
                                self.parse_page()
                                i += 1
                            self.batch_transfer(id_info)
                        if info['transferred'] >= info['expect_uc']:
                            self.transfer_num += 1
                    self.id_info = id_info
            else:
                sleep(300)

    def batch_transfer(self, id_info):
        info = self.info[id_info]
        if 'expect_uc' not in info:
            if isinstance(UC, (tuple, list)):
                if len(UC) == 2:
                    info['expect_uc'] = random.randint(UC[0], UC[1])
                else:
                    if isfunction(UC[2]):
                        info['expect_uc'] = UC[2](random.uniform(UC[0], UC[1]))
                    else:
                        n = (UC[2] - UC[1]) / (UC[1] - UC[0])
                        x = pow((UC[2] - UC[0]) / 10000, 1/n)
                        info['expect_uc'] = UC[0] + int(round((random.uniform(0, x) ** n) * 10000))
            else:
                info['expect_uc'] = UC
            self.save()

        uc = info['expect_uc'] - info['transferred']
        uid = info['transfer_uid'] if info['transfer_uid'] > 0 else info['post_uid']
        info_msg = f"{self.page_info} | {id_info} | 计划转账 {info['expect_uc']} UCoin"
        msg = f"{info_msg}{' | ' + MSG if MSG else ''}" if INFO else MSG
        logger.info(info_msg)

        cost = uc * 1.5 + (int(uc / 50000) + 1) * 100
        if self.uc_amount < cost:
            logger.warning(f"{id_info} | UCoin 不足 | {self.uc_amount} < {cost} | {'退出程序' if EXT else '等待'}")
            if EXT:
                exit()
            else:
                sleep(900)
                self.parse_page()
                self.batch_transfer(id_info)

        times = 0
        if RE != -1:
            for _id_info, _info in self.info.items():
                if _id_info != id_info and 'transfer_uid' in _info:
                    _uid = _info['transfer_uid'] if _info['transfer_uid'] > 0 else _info['post_uid']
                    if _uid == uid and _info['transferred'] >= _info['expect_uc']:
                        times += 1
            if times >= RE:
                logger.info(f"{id_info} | 已经给用户 {uid} 转账 {times} 次，跳过")
                return

        while uc > 0:
            data = {'event': '1003', 'recv': uid, 'amount': 50000 if uc >= 50000 else uc, 'message': msg}
            retries = 4
            for _ in range(retries + 1):
                try:
                    page = requests.post('https://u2.dmhy.org/mpshop.php', **R_ARGS, data=data).text
                    soup = BeautifulSoup(page.replace('\n', ''), 'lxml')
                    if soup.h2 and soup.h2.text in ('Error', '错误', '錯誤', 'Ошибка'):
                        err_msg = soup.select('table td.text')[1].text
                        logger.error(f"{id_info} | 转账发生错误: {err_msg} | data: {data}")
                        delay = re.findall(r'(\d+)', err_msg)
                        if delay and int(delay[0]) <= 300:
                            logger.info(f'将在 {int(delay[0])} 秒后重试')
                            sleep(int(delay[0]))
                    else:
                        uc -= data['amount']
                        info['transferred'] += data['amount']
                        self.save()
                        logger.info(f"{id_info} | 成功给用户 {uid} 转账 {data['amount']} UCoin")
                        sleep(300)
                        break
                except Exception as er:
                    logger.error(f"{id_info} | 转账发生错误: {er} | data: {data}")
                if _ == retries:
                    return

    def parse_page(self):
        url = f'{self.url}&page={self.index}'
        soup = self.get_soup(url)

        td = soup.find('table', {'border': '1'}).td
        for tag in td:
            if tag.name == 'div' and tag['style'].startswith('margin-top'):
                id_info = tag.table['id']
                user_details = tag.select("a[href^='userdetails.php?id=']")
                uid = int(user_details[0]['href'][19:]) if user_details else None

                if id_info not in self.info:
                    self.info[id_info] = {'post_uid': uid, 'transferred': 0}
                    if uid is None:
                        logger.info(f'{id_info} | 用户匿名，无法发糖')
                    elif uid == self.uid:
                        logger.info(f'{id_info} | 用户为自己，跳过')

            elif tag.name == 'table':
                if self.info[id_info]['transferred'] > 0 or uid in (self.uid, None):
                    continue
                if TEXT:
                    self.info[id_info]['text'] = self.strip_content(tag.select('span bdo')[0])

                self.validate_uid(id_info)

        self.save()
        _list = list(self.info.keys())
        if _list and _list[len(_list) - 1] == self.id_info:
            all_p = soup.find_all('p', {'align': 'center'})
            if all_p[0].contents[2].name == 'a':
                self.index += 1
                self.parse_page()
            elif all_p[1].next_sibling.name == 'p':
                logger.info('所有楼层已发完，帖子已被锁定，退出程序')
                exit()

    def validate_uid(self, id_info):
        if TEXT:
            valid = True
            all_id = re.findall(RGX, self.info[id_info]['text'])
            uid = int(all_id[0]) if all_id else self.info[id_info]['post_uid']

            def find_uid():
                nonlocal valid

                _list = [self.page_info]
                _list.extend([page_info for page_info in self.total_info.keys() if page_info != self.page_info])
                for page_info in _list:
                    for _id_info, _info in self.total_info[page_info].items():
                        transfer_uid = _info.get('transfer_uid')
                        if transfer_uid in [uid, -uid]:
                            valid = True if transfer_uid > 0 else False
                            return
                        if _info['post_uid'] == uid:
                            valid = True
                            return

                if self.get_soup(f'https://u2.dmhy.org/userdetails.php?id={uid}'
                                 ).find('td', {'id': 'outer', 'align': 'center'}).h1:
                    valid = True
                else:
                    valid = False

            find_uid()
            if self.info[id_info].get('transfer_uid') not in [uid, -uid]:
                if valid:
                    self.info[id_info]['transfer_uid'] = uid
                    if all_id:
                        logger.info(f'{id_info} | 解析到用户 ID {uid}，将会给用户 {uid} 发糖')
                    else:
                        logger.info(f'{id_info} | 没有解析到用户 ID，将会给层主 {uid} 发糖')
                else:
                    self.info[id_info]['transfer_uid'] = -uid
                    logger.info(f"{id_info} | {uid} 不是有效的用户 ID，将会给层主 {self.info[id_info]['post_uid']} 发糖")
        else:
            if 'transfer_uid' not in self.info[id_info]:
                self.info[id_info]['transfer_uid'] = self.info[id_info]['post_uid']
                logger.info(f"{id_info} | 将会给用户 {self.info[id_info]['post_uid']} 发糖")

    @staticmethod
    def strip_content(element):
        contents = []

        def _strip_content(_element):
            if isinstance(_element, bs4.element.Tag):
                if _element.name == 'fieldset' and _element.legend:  # 排除引用、Media Info
                    return
                if _element.name == 'div' and _element.get('class') in [['codemain'], ['codetop']]:  # 排除代码
                    return
                if _element.name == 'a' and _element.get('class') == ['faqlink']:  # 排除链接
                    return
                if _element.name in ['img', 'button']:  # 排除图片、折叠按钮(没有排除折叠内容)
                    return
                for child_element in _element.contents:
                    _strip_content(child_element)
            else:
                contents.append(str(_element))

        _strip_content(element)
        return ' '.join(contents)

    def print_info(self):
        idx = 0
        fin_idx = 0
        contents = []
        for id_info, info in self.info.items():
            if info['post_uid'] not in [self.uid, None]:
                idx += 1
                ts = info['transferred']
                if ts == 0:
                    _list = list(self.info.keys())
                    if _list.index(id_info) > _list.index(self.id_info):
                        state = '未开始' if NUM == -1 or fin_idx < NUM else '已取消'
                    else:
                        state = '失败'
                else:
                    state = '未完成' if ts < info['expect_uc'] else '已完成'
                if state == '已完成':
                    fin_idx += 1
                contents.append(f"{idx} | {fin_idx if state == '已完成' else '无'} | {id_info} | {info['post_uid']} | "
                                f"{info['transfer_uid'] if info['transfer_uid'] > 0 else info['post_uid']} | "
                                f"{ts} | {info['text']} | {state}")
        info_str = '\n'.join(contents)

        logger.info(f'-------------{self.page_info} 转账信息----------------\n'
                    f'转账序号 | 完成序号 | 楼层 ID | 回复者 UID | 转账 UID | 转账金额 | 回复内容 | 转账状态\n'
                    f'{info_str}')


logger.add(level='DEBUG', sink=LOG_PATH)
t = TransferUCoin()
while True:
    try:
        t.run()
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            t.print_info()
            break
        else:
            logger.exception(e)
