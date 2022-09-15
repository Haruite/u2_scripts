"""发糖脚本，用于在论坛或者种子评论区发糖，没有实际测试过
解析回复内容会自动去掉引用、代码、链接，但不会去掉折叠内容
不想吐槽这个代码，明明就是这么简单的功能因为抠逻辑写得难读得屎一样"""

import json
import os
import re
from time import sleep

import bs4.element
import requests

from loguru import logger
from bs4 import BeautifulSoup

R_ARGS = {'cookies': {'nexusphp_u2': ''},  # 网站 cookie
          'headers': {'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
                                    'Chrome/103.0.5060.134 Safari/537.36 Edg/103.0.1264.77'},
          'proxies': {'http': '', 'https': ''},  # 代理
          'timeout': 20,
          'verify': True,
          }  # requests 模块参数
URL = ''  # 帖子、种子、候选的 url，直接复制即可
UC = 100000  # 每人转账 uc 数量
NUM = -1  # 发糖人数， -1 表示不限制
TEXT = True  # 是否解析回帖内容，如果不解析一律发给回复者本人，否则的话如果发给回复中解析出有效的用户 id (没有还是发给本人)
RGX = r'(\d{2}\d+)'  # 从回复内容中解析发糖 uid 的正则表达式
RE = 1  # 同一个用户最大转账次数(一条评论算一次)，-1 为不限制
EXT = True  # 为真时 uc 不足直接退出脚本，否则等到 uc 恢复继续发糖
MSG = ''  # 留言
INFO = False  # 是否在留言中注明帖子和评论 id 等信息
UPDATE = False  # 为真时每次给一个人发糖前都会检查帖子内容，否则等所有人发完了再检查帖子内容
DATA_PATH = f'{os.path.splitext(__file__)[0]}.info'
LOG_PATH = f'{os.path.splitext(__file__)[0]}.log'


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
                    self.uc_amount = float(
                        info_block.find('span', {'class': 'ucoin-notation ucoin-collapsed'})['title'].replace(',', ''))
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
                return

            self.parse_page()
            _list = list(self.info.keys())
            index = (-1 if not self.id_info else _list.index(self.id_info)) + 1
            i = 0

            if len(_list) > index:
                for id_info in _list[index:]:
                    info = self.info[id_info]
                    if info['post_uid'] not in [self.uid, None]:
                        if info['transferred'] < UC:
                            if i > 0 and UPDATE:
                                self.parse_page()
                                i += 1
                            self.batch_transfer(id_info, info)
                        if info['transferred'] >= UC:
                            self.transfer_num += 1
                    self.id_info = id_info
            else:
                sleep(300)

    def batch_transfer(self, id_info, info):
        uc = UC - info['transferred']
        uid = info['transfer_uid'] if info['transfer_uid'] > 0 else info['post_uid']
        msg = f"{self.page_info} | {id_info}{' | ' + MSG if MSG else ''}" if INFO else MSG

        if self.uc_amount < uc * 1.5:
            logger.warning(f'{id_info} | UCoin 不足 | {self.uc_amount} < {uc * 1.5:.2f}')
            if EXT:
                exit()
            else:
                sleep(900)
                self.parse_page()
                self.batch_transfer(id_info, info)

        times = 0
        if RE != -1:
            for _id_info, _info in self.info.items():
                if _id_info != id_info and 'transfer_uid' in _info:
                    _uid = _info['transfer_uid'] if _info['transfer_uid'] > 0 else _info['post_uid']
                    if _uid == uid and _info['transferred'] >= UC:
                        times += 1
            if times >= RE:
                logger.info(f"{id_info} | 已经给用户 {uid} 转账 {times} 次，跳过")
                return

        while uc > 0:
            data = {'event': '1003', 'recv': uid, 'amount': 50000 if uc >= 50000 else uc, 'message': msg}
            for _ in range(5):
                try:
                    page = requests.post('https://u2.dmhy.org/mpshop.php', **R_ARGS, data=data).text
                    soup = BeautifulSoup(page.replace('\n', ''), 'lxml')
                    if soup.h2 and soup.h2.text in ('Error', '错误', '錯誤', 'Ошибка'):
                        logger.error(f"{id_info} | 转账发生错误: {soup.select('table td.text')[1].text} | data: {data}")
                    else:
                        uc -= data['amount']
                        info['transferred'] += data['amount']
                        self.save()
                        logger.info(f"{id_info} | 成功给用户 {uid} 转账 {data['amount']} UCoin")
                        sleep(300)
                        break
                except Exception as er:
                    logger.error(f"{id_info} | 转账发生错误: {er} | data: {data}")
                if _ == 4:
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
            if soup.find('p', {'align': 'center'}).contents[2].name == 'a':
                self.index += 1
                self.parse_page()

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
            if not self.info[id_info].get('transfer_uid'):
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


logger.add(level='DEBUG', sink=LOG_PATH)
t = TransferUCoin()
while True:
    try:
        t.run()
        break
    except Exception as e:
        logger.exception(e)
