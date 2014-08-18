# -*- coding: utf-8 -*-
"""
    website.vpn.sts.services
    ~~~~~~~~~~~~~~~~~~~~~~~~

    vpn site-to-site services api.

    :copyright: (c) 2014 by xiong.xiaox(xiong.xiaox@alibaba-inc.com).
"""


import json
import time

from flask import render_template, flash

from website import db
from website.services import exec_command
from website.vpn.sts.models import Tunnels


class VpnConfig(object):
    ''' read and set config for vpn config file.'''
    secrets_file = '/etc/strongswan/ipsec.secrets'
    conf_file = '/etc/strongswan/ipsec.conf'

    secrets_template = 'sts/ipsec.secrets'
    conf_template = 'sts/ipsec.conf'

    _auth_types = ['secret']

    def __init__(self, conf_file=None, secrets_file=None):
        if conf_file:
            self.conf_file = conf_file
        if secrets_file:
            self.secrets_file = secrets_file

    def _get_tunnels(self):
        data = Tunnels.query.all()
        if data:
            return [{'id': item.id, 'name': item.name,
                     'psk': item.psk, 'rules': json.loads(item.rules)}
                    for item in data]
        return None

    def _commit_conf_file(self):
        tunnels = self._get_tunnels()
        data = render_template(self.conf_template, tunnels=tunnels)
        try:
            with open(self.conf_file, 'w') as f:
                f.write(data)
        except:
            return False
        return True

    def _commit_secrets_file(self):
        data = self._get_tunnels()
        tunnels = [{'leftid': i['rules']['leftid'],
                    'rightid': i['rules']['rightid'],
                    'psk': i['psk']} for i in data]
        data = render_template(self.secrets_template, tunnels=tunnels)
        try:
            with open(self.secrets_file, 'w') as f:
                f.write(data)
        except:
            return False
        return True

    def update_tunnel(self, tunnel_id, tunnel_name, rules, psk):
        #: store to instance
        self.tunnel = Tunnels.query.filter_by(id=tunnel_id).first()
        if self.tunnel is None:
            self.tunnel = Tunnels(tunnel_name, rules, psk)
            db.session.add(self.tunnel)
        else:
            self.tunnel.name = tunnel_name
            self.tunnel.rules = rules
            self.tunnel.psk = psk
        db.session.commit()
        return True

    def delete(self, id):
        tunnel = Tunnels.query.filter_by(id=id).first()
        db.session.delete(tunnel)
        db.session.commit()
        return True

    def commit(self):
        if self._commit_conf_file() and self._commit_secrets_file():
            return True
        return False


class VpnServer(object):
    """vpn server console"""
    def __init__(self):
        self.cmd = None
        self.c_code = None
        self.c_stdout = None
        self.c_stderr = None

    def __repr__(self):
        return '<VpnServer %s:%s:%s:%s>' % (self.cmd, self.c_code,
                                            self.c_stdout, self.c_stderr)

    def _exec(self, cmd, message=None):
        try:
            r = exec_command(cmd)
        except:
            flash(u'VpnServer 程序异常，无法调用，请排查操作系统相关设置！', 'alert')
            return False
        #: store cmd info
        self.cmd = cmd
        self.c_code = r['return_code']
        self.c_stdout = r['stdout']
        self.c_stderr = r['stderr']
        #: check return code
        if r['return_code'] == 0:
            return True
        if message:
            flash(message % r['stderr'], 'alert')
        return False

    def _tunnel_exec(self, cmd, message=None):
        if not self._exec(cmd, message):
            return False
        #: check return data
        try:
            r = self.c_stdout[-1]
        except IndexError:
            flash(u'命令已执行，但是没有返回数据。', 'alert')
            return False
        #: check return status
        if 'successfully' in r:
            return True
        else:
            message = u'命令已执行，但是没有返回成功状态：%s' % r
            flash(message, 'alert')
            return False

    def _reload_conf(self):
        cmd = ['strongswan', 'reload']
        message = u"VPN 服务配置文件加载失败：%s"
        return self._exec(cmd, message)

    def _rereadsecrets(self):
        cmd = ['strongswan', 'rereadsecrets']
        message = u"VPN 服务密钥文件加载失败：%s"
        return self._exec(cmd, message)

    @property
    def start(self):
        cmd = ['strongswan', 'start']
        message = u"VPN 服务启动失败：%s"
        return self._exec(cmd, message)

    @property
    def stop(self):
        cmd = ['strongswan', 'stop']
        message = u"VPN 服务停止失败：%s"
        return self._exec(cmd, message)

    @property
    def reload(self):
        tunnel = VpnConfig()
        if not tunnel.commit():
            message = u'VPN 服务配置文件下发失败，请重试。'
            flash(message, 'alert')
            return False
        if self._reload_conf() and self._rereadsecrets():
            return True
        return False

    @property
    def status(self):
        cmd = ['strongswan', 'status']
        return self._exec(cmd)

    def tunnel_status(self, tunnel_name):
        cmd = ['strongswan', 'status', tunnel_name]
        if self._exec(cmd):
            for item in self.c_stdout:
                if 'INSTALLED' in item:
                    return True
        return False

    def tunnel_up(self, tunnel_name):
        if self.tunnel_status(tunnel_name):
            flash(u'隧道已经连接！', 'info')
            return False
        cmd = ['strongswan', 'up', tunnel_name]
        message = u"隧道启动失败：%s"
        return self._tunnel_exec(cmd, message)

    def tunnel_down(self, tunnel_name):
        if not self.tunnel_status(tunnel_name):
            flash(u'隧道已经断开！', 'info')
            return False
        #: use self.tunnel_status() return stdout[-1] to get tunnel real name
        cmd = ['strongswan', 'down', self.c_stdout[-1].split(':')[0].strip()]
        message = u"隧道停止失败：%s"
        return self._tunnel_exec(cmd, message)

    def tunnel_traffic(self, tunnel_name):
        cmd = ['strongswan', 'statusall', tunnel_name]
        rx_pkts = 0
        tx_pkts = 0
        if self._exec(cmd):
            raw_data = self.c_stdout[-2].replace(',', '').split()
            if raw_data[raw_data.index('bytes_i')+1].startswith('('):
                #: check Timestamp > 2s, then drop.
                if int(raw_data[raw_data.index('bytes_i')+3].strip('s')) < 2:
                    tx_pkts = raw_data[raw_data.index('bytes_i')+1].strip('(')
            if raw_data[raw_data.index('bytes_o')+1].startswith('('):
                #: check Timestamp > 2s, then drop.
                if int(raw_data[raw_data.index('bytes_o')+3].strip('s')) < 2:
                    rx_pkts = raw_data[raw_data.index('bytes_o')+1].strip('(')
            return {'rx_pkts': int(rx_pkts),
                    'tx_pkts': int(tx_pkts),
                    'time': int(time.time())}
        return False


def vpn_settings(form, tunnel_id=None):
    tunnel = VpnConfig()
    vpn = VpnServer()
    rules = {'auto': form.start_type.data, 'esp': form.protocol_type.data,
             'left': form.local_ip.data, 'leftsubnet': form.local_subnet.data,
             'leftid': form.tunnel_name.data, 'right': form.remote_ip.data,
             'rightsubnet': form.remote_subnet.data, 'rightid': form.tunnel_name.data,
             'authby': 'secret'}
    if tunnel.update_tunnel(tunnel_id, form.tunnel_name.data, json.dumps(rules),
                            form.psk.data) and vpn.reload:
        return True
    return False


def vpn_del(id):
    config = VpnConfig()
    vpn = VpnServer()
    tunnel = get_tunnels(id, True)[0]
    if tunnel['status']:
        vpn.tunnel_down(tunnel['name'])
    if config.delete(id) and vpn.reload:
        return True
    return False


def get_tunnels(id=None, status=False):
    if id:
        data = Tunnels.query.filter_by(id=id)
    else:
        data = Tunnels.query.all()
    if data:
        tunnels = [{'id': item.id, 'name': item.name, 'psk': item.psk,
                    'rules': json.loads(item.rules)} for item in data]
        if status:
            vpn = VpnServer()
            for tunnel in tunnels:
                tunnel['status'] = vpn.tunnel_status(tunnel['name'])
        return tunnels
    return None
