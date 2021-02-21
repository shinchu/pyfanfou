#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author: mcxiaoke
# @Date:   2015-08-06 07:23:50
from __future__ import print_function
import sys
import os
import argparse
from . import utils
import time
from .api import ApiClient
from .api import ApiError
from .db import DB
import os
import json
import logging
from . import renderer
from requests import ConnectionError
from multiprocessing import Pool
from multiprocessing.dummy import Pool as ThreadPool

'''
饭否数据处理脚本
'''

DEFAULT_COUNT = 60
DEFAULT_USER_COUNT = 100

__version__ = '1.0.0'
__stdout__ = sys.stdout

logger = logging.getLogger(__name__)


class Backup(object):

    def __init__(self, **options):
        '''
        备份指定用户的饭否消息数据
        '''
        logger.info('Backup.init()', options)

        self._parse_options(**options)
        self.api = ApiClient(False)
        self.token = utils.load_account_info(self.username)
        self.user = None
        self.target_id = None
        self.db = None
        self.cancelled = False
        self.total = 0
        self.user_total = 0
        self.photo_total = 0

    def _parse_options(self, **options):
        '''
        username - 用户帐号（可选）
        password - 用户密码（可选）
        output - 数据保存目录
        target - 目标用户ID
        include_user - 是否备份好友资料
        include_photo - 是否备份相册照片
        '''
        self.username = options.get('username')
        self.password = options.get('password')
        self.auth_mode = self.username and self.password
        self.target = options.get('target')
        self.output = os.path.abspath(
            options.get('output') or os.path.join('output'))
        self.include_user = options.get('include_user')
        self.include_photo = options.get('include_photo')

    def _precheck(self):
        if self.token:
            print('载入{1}的本地登录信息{0}'.format(
                self.token['oauth_token'], self.username))
            self.api.set_oauth_token(self.token)
        if self.auth_mode:
            if self.api.is_verified():
                self.token = self.api.oauth_token
                self.user = self.api.user
            else:
                self.token = self.api.login(self.username, self.password)
                self.user = self.api.user
                print('保存{1}的登录信息{0}'.format(
                    self.token['oauth_token'], self.username))
                utils.save_account_info(self.username, self.token)
        if not self.target and not self.user:
            print('没有指定备份的目标用户')
            return
        self.target_id = self.target or self.user['id']

    def stop(self):
        print('收到终止备份的命令，即将停止...')
        self.cancelled = True

    def start(self):
        self._precheck()
        if not self.target_id:
            return
        try:
            self.target_user = self.api.get_user(self.target_id)
        except ApiError as e:
            if e.args[0] == 404:
                print('你指定的用户{0}不存在'.format(self.target_id))
            self.target_user = None
        if not self.target_user:
            print(
                '无法获取用户{0}的信息'.format(self.target_id))
            return
        print('用户{0}共有{1}条消息，{2}个好友'.format(
            self.target_user['id'],
            self.target_user['statuses_count'],
            self.target_user['friends_count']))
        if not os.path.exists(self.output):
            os.mkdir(self.output)
        print('开始备份用户{0}的消息...'.format(self.target_id))
        db_file = os.path.abspath(
            '{0}/{1}.db'.format(self.output, self.target_id))
        print('保存路径：{0}'.format(self.output))
        self.db = DB(db_file)
        db_count = self.db.get_status_count()
        if db_count:
            print('发现数据库已备份消息{0}条'.format(db_count))
        # first ,check new statuses
        self._fetch_newer_statuses()
        # then, check older status
        self._fetch_older_statuses()
        if self.include_photo:
            # check user photos
            print('开始备份用户{0}的相册照片...'.format(self.target_id))
            start = time.time()
            self._fetch_photos_multi()
            elasped = time.time()-start
            print('备份用户{0}的照片共耗时{1}秒'.format(self.target_id, elasped))
        if self.include_user:
            # check user followings
            print('开始备份用户{0}的好友资料...'.format(self.target_id))
            self._fetch_followings()
        self._render_statuses()
        self._report()
        if self.cancelled:
            print('本次备份已终止')
        else:
            print('本次备份已完成')
        self.db.close()

    def _report(self):
        print('本次共备份了{1}的{0}条消息'.format(
            self.total, self.target_id))
        print('本次共备份了{1}的{0}张照片'.format(
            self.photo_total, self.target_id))
        print('本次共备份了{1}的{0}个好友'.format(
            self.user_total, self.target_id))

    def _render_statuses(self):
        db_data = self.db.get_all_status()
        if db_data:
            data = []
            print('开始读取{0}的消息列表数据...'.format(self.target_id))
            for dt in db_data:
                data.append(json.loads(dt['data']))
            fileOut = os.path.join(
                self.output, self.target_id)
            print('开始导出{0}的消息列表为Html/Markdown/Txt...'.format(self.target_id))
            renderer.render(data, fileOut)
            print('已导出文件', fileOut+'.html|.md|.txt')

    def _fetch_followings(self):
        '''全量更新，获取全部好友数据'''
        page = 0
        while(not self.cancelled):
            users = self.api.get_friends(self.target_id, page=page)
            if not users:
                break
            count = len(users)
            print("正在保存第{0}-{1}条用户资料 ...".format(
                self.user_total, self.user_total+count))
            self.db.bulk_insert_user(users)
            self.user_total += count
            page += 1
            time.sleep(1)
            if len(users) < DEFAULT_USER_COUNT:
                break

    def _download_photo(self, status):
        photo = status['photo']
        status_id = status['id']
        if photo:
            url = photo['largeurl']
            img_dir = os.path.join(
                self.output, '{0}-photos'.format(self.target_id))
            if not os.path.exists(img_dir):
                os.mkdir(img_dir)
            img_name = '{0}.{1}'.format(status_id, url[-3:] or 'jpg')
            filename = os.path.join(img_dir, img_name)
            if os.path.exists(filename):
                print('照片已存在 {0}'.format(img_name))
            else:
                print('正在下载照片 {0}'.format(img_name))
                utils.download_and_save(url, filename)

    def _fetch_photos_multi(self):
        rows = self.db.get_photo_status()
        if not rows:
            print('{0}的相册里没有照片'.format(self.target_id))
            return
        photos = []
        for row in rows:
            photos.append(json.loads(row['data']))

        count = len(photos)
        print("正在下载第{0}-{1}张照片 ...".format(
            self.photo_total, self.photo_total+count))
        pool = ThreadPool(8)
        try:
            pool.map(self._download_photo, photos)
            pool.close()
            pool.join()
            self.photo_total += count
        except KeyboardInterrupt:
            pool.terminate()

    def _fetch_photos(self):
        rows = self.db.get_photo_status()
        if not rows:
            print('{0}的相册里没有照片'.format(self.target_id))
            return
        photos = []
        for row in rows:
            photos.append(json.loads(row['data']))
        count = len(photos)
        print("正在下载第{0}-{1}张照片 ...".format(
            self.photo_total, self.photo_total+count))
        for photo in photos:
            if self.cancelled:
                break
            self._download_photo(photo)
        self.photo_total += count

    def _fetch_photos_old(self):
        photos = self.db.get_photo_status()
        print('photos', len(photos))
        tail_status = None
        while(not self.cancelled):
            max_id = tail_status['id'] if tail_status else None
            timeline = self.api.get_user_photos(
                self.target_id, count=DEFAULT_COUNT, max_id=max_id)
            if not timeline:
                break
            tail_status = timeline[-1]
            count = len(timeline)
            print("正在下载第{0}-{1}张照片 ...".format(
                self.photo_total, self.photo_total+count))
            for status in timeline:
                if self.cancelled:
                    break
                self._download_photo(status)
            self.photo_total += count
            if len(timeline) < DEFAULT_COUNT:
                break

    def _fetch_newer_statuses(self):
        '''增量更新，获取比某一条新的数据（新发布的）'''
        head_status = self.db.get_latest_status()
        if not head_status:
            return
        while(not self.cancelled):
            head_status = self.db.get_latest_status()
            since_id = head_status['sid'] if head_status else None
            error = None
            retry = 0
            while retry < 3:
                try:
                    timeline = self.api.get_user_timeline(
                        self.target_id, count=DEFAULT_COUNT,
                        since_id=since_id)
                    break
                except ConnectionError as e:
                    error = e
                    print(e)
                    timeline = None
                    print('网络连接超时，即将尝试第{0}重试...'.format(retry+1))
                    time.sleep(retry*5)
                    retry += 1
            if error:
                raise error
            if not timeline:
                break
            count = len(timeline)
            print("正在保存第{0}-{1}条消息，共{2}条 ...".format(
                self.total, self.total+count,
                self.target_user['statuses_count']))
            self.db.bulk_insert_status(timeline)
            self.total += count
            time.sleep(1)
            if len(timeline) < DEFAULT_COUNT:
                break

    def _fetch_older_statuses(self):
        '''增量更新，获取比某一条旧的数据'''
        while not self.cancelled:
            tail_status = self.db.get_oldest_status()
            max_id = tail_status['sid'] if tail_status else None
            error = None
            retry = 0
            while retry < 3:
                try:
                    timeline = self.api.get_user_timeline(
                        self.target_id, count=DEFAULT_COUNT, max_id=max_id)
                    break
                except ConnectionError as e:
                    error = e
                    print(e)
                    timeline = None
                    print('网络连接超时，即将尝试第{0}重试...'.format(retry+1))
                    time.sleep(retry*5)
                    retry += 1
            if error:
                raise error
            if not timeline:
                break
            count = len(timeline)
            print("正在保存第{0}-{1}条消息，共{2}条 ...".format(
                self.total, self.total+count,
                self.target_user['statuses_count']))
            self.db.bulk_insert_status(timeline)
            self.total += count
            time.sleep(1)
            if len(timeline) < DEFAULT_COUNT:
                break


def parse_args():
    '''
    解析命令行参数
    '''
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='饭否数据备份工具 v{0}'.format(__version__),
        epilog='''项目主页 https://github.com/mcxiaoke/pyfanfou
        ''')
    parser.add_argument('-u', '--username',
                        help='你的饭否帐号')
    parser.add_argument('-p', '--password',
                        help='你的饭否密码')
    parser.add_argument('-t', '--target',
                        help='要备份的用户ID，默认是登录帐号')
    parser.add_argument('-s', '--include-user', action='store_const',
                        const=False, help='是否备份好友资料列表，默认否')
    parser.add_argument('-i', '--include-photo', action='store_const',
                        const=True, help='是否备份全部相册照片，默认是')
    parser.add_argument('-o', '--output',
                        help='备份数据存放目录，默认是当前目录下的output目录')
    # parser.add_argument('-v', '--view', action='store_false',
    #                   help='仅显示命令行参数值，不执行操作')
    args = parser.parse_args()
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)
    return args

if __name__ == '__main__':
    Backup(**vars(parse_args())).start()
