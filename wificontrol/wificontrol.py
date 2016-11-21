# wificontrol code is placed under the GPL license.
# Written by Ivan Sapozhkov (ivan.sapozhkov@emlid.com)
# Copyright (c) 2016, Emlid Limited
# All rights reserved.

# If you are interested in using wificontrol code as a part of a
# closed source project, please contact Emlid Limited (info@emlid.com).

# This file is part of wificontrol.

# wificontrol is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# wificontrol is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with wificontrol.  If not, see <http://www.gnu.org/licenses/>.

import re
import subprocess
from sysdmanager import SystemdManager
from threading import Thread, Event, Timer
from utils import ConfigurationFileUpdater, NullFileUpdater
from utils import WpaSupplicantInterface, WpaSupplicantNetwork, WpaTemplates
from utils import FileError
from utils import ServiceError, InterfaceError, PropertyError

class ConnectionError(Exception):
    pass

class WiFiControl(object):
    _default_path = {
        'hostapd_path': "/etc/hostapd/hostapd.conf",
        'wpa_supplicant_path': "/etc/wpa_supplicant/wpa_supplicant.conf",
        'p2p_supplicant_path': "/etc/wpa_supplicant/p2p_supplicant.conf",
        'hostname_path': '/etc/hostname'
    }
    _wpas_control = lambda action: "systemctl {} wpa_supplicant.service && sleep 2".format(action)
    _hostapd_control = lambda action: "systemctl {} hostapd.service && sleep 2".format(action)
    _launch_restart_mdns = "systemctl restart mdns.service && sleep 2"
    _rfkill_wifi_control = lambda action: "rfkill {} wifi".format(action)

    def __init__(self, interface='wlan0'):
        self.hostapd_path = self._default_path['hostapd_path']
        self.wpa_supplicant_path = self._default_path['wpa_supplicant_path']
        self.p2p_supplicant_path = self._default_path['p2p_supplicant_path']
        self.hostname_path = self._default_path['hostname_path']
        self.interface = interface

        try:
            self._launch("wpa_supplicant")
        except OSError:
            raise OSError('No WPA_SUPPLICANT servise')
        except subprocess.CalledProcessError:
            try:
                self._launch("hostapd")
            except OSError:
                raise OSError('No HOSTAPD servise')
            except subprocess.CalledProcessError:
                pass

        self._systemd_manager = SystemdManager()
        self._wpa_supplicant_interface = WpaSupplicantInterface(self.interface)
        self._wpa_network_manage = WpaSupplicantNetwork()
        try:
            self._config_updater = ConfigurationFileUpdater(self.wpa_supplicant_path)
        except FileError:
            self._config_updater = NullFileUpdater()

        self._connection_thread = None
        self._connection_timer = None
        self._break_event = Event()
        self._connection_event = Event()
        self._network_list = None
        
        self._wpa_supplicant_start = lambda: self._systemd_manager.is_active("wpa_supplicant.service")
        self._hostapd_start = lambda: self._systemd_manager.is_active("hostapd.service")
        self._wifi_on = lambda: (self._wpa_supplicant_start or self._hostapd_start)

        if self._wpa_supplicant_start:
            self._wpa_supplicant_interface.initialize()

    def start_host_mode(self):
        self._launch(self._wpas_control("stop"))
        self._launch(self._hostapd_control("start"))

    def start_client_mode(self):
        self._launch(self._hostapd_control("stop"))
        self._launch(self._wpas_control("start"))
        self._wpa_supplicant_interface.initialize()
        self._wpa_supplicant_interface.reconnect()
        
    def turn_on_wifi(self):
        self._launch(self._rfkill_wifi_control("unblock"))
        self._launch(self._wpas_control("start"))

    def turn_off_wifi(self):
        self._launch(self._wpas_control("stop"))
        self._launch(self._hostapd_control("stop"))
        self._launch(self._rfkill_wifi_control("block"))

    def get_wifi_turned_on(self):
        return self._wifi_on

    def get_hostap_name(self):
        try:
            return self._launch(
                "grep \'^ssid=\' {}".format(
                    self.hostapd_path))[5:-1]
        except subprocess.CalledProcessError:
            return None

    def set_hostap_password(self, password):
        try:
            self._launch(
                "sed -i s/^wpa_passphrase=.*/wpa_passphrase={}/ {}".format(
                    password, self.hostapd_path))
        except subprocess.CalledProcessError:
            return False
        else:
            return True

    def get_device_name(self):
        return self._get_host_name()

    def set_device_names(self, name):
        self._set_hostap_name(name)
        self._set_p2p_name(name)
        self._set_host_name(name)
        try:
            self._launch(self._launch_restart_mdns)
        except subprocess.CalledProcessError:
            return False
        else:
            return True

    def get_status(self):
        network_params = dict()
        try:
            network_params['ssid'] = self._get_current_network_ssid()
        except PropertyError:
            network_params = None
        else:
            network_params['mac address'] = self._get_device_mac()
            network_params['IP address'] = self._get_device_ip()
        finally:
            network_state = (self._get_state(), network_params)
            return network_state

    def get_added_networks(self):
        return self._config_updater.networks

    def add_network(self, network_parameters):
        network = dict(WpaTemplates(network_parameters))
        try:
            self._config_updater.addNetwork(network)
        except AttributeError:
            pass
        else:
            if self._wpa_supplicant_start:
                self._wpa_supplicant_interface.addNetwork(network)

    def remove_network(self, network):
        try:
            self._config_updater.removeNetwork(network)
        except AttributeError:
            pass
        else:
            if self._wpa_supplicant_start:
                self._wpa_network_manage.removeNetwork(self._find_remove_network_path(network))

    def start_connecting(self, network, callback=None,
                         args=None, timeout=10, any_network=False):
        self._break_connecting()
        self.start_client_mode()
        self._choose_thread(network, callback, args, any_network)
        self.__start_connecting_thread(timeout)

    def connect(self, network, callback=None, any_network=False, args=None):
        
        result = self._connect_to_network(network)
        self._teardown_connection()
        if callback is not None:
            if args is not None:
                callback(result, args)
            else:
                callback(result)

    def stop_connecting(self):
        self._connection_event.clear()
        self._connection_thread.join()

    def disconnect(self):
        self._wpa_supplicant_interface.disconnect()

    ### Protected methods 
    # Names changung actions
    def _set_hostap_name(self, name='reach'):
        try:
            mac_addr = self._get_device_mac()[-6:]
            self._launch(
                "sed -i s/^ssid=.*/ssid={}{}/ {}".format(name, mac_addr,
                                                         self.hostapd_path))
        except subprocess.CalledProcessError:
            return False
        else:
            return True

    def _set_host_name(self, name='reach'):
        try:
            hostname_file = open(self.hostname_path, 'w')
        except IOError:
            return False
        else:
            hostname_file.write(name + '\n')
            hostname_file.close()
            try:
                self._launch('hostname -F {}'.format(self.hostname_path))
            except subprocess.CalledProcessError:
                return False
            else:
                return True

    def _get_host_name(self):
        try:
            return self._launch("cat {}".format(self.hostname_path)).strip()
        except subprocess.CalledProcessError:
            return None

    def _set_p2p_name(self, name='reach'):
        try:
            self._launch(
                "sed -i s/^p2p_ssid_postfix=.*/p2p_ssid_postfix={}/ {}".format(
                    name, self.p2p_supplicant_path))
        except subprocess.CalledProcessError:
            return False
        else:
            return True

    def _get_p2p_name(self):
        try:
            return self._launch(
                "grep \'^p2p_ssid_postfix=\' {}".format(
                    self.p2p_supplicant_path))[17:-1]
        except subprocess.CalledProcessError:
            return None
    
    # Network actions
    def _find_remove_network_path(self, aim_network):
        for network in self._wpa_supplicant_interface.getNetworks():
            if self.self._wpa_network_manage.getNetworkSSID(network) == aim_network['ssid']:
                return network

    def _get_current_network_ssid(self):
        network = self._wpa_supplicant_interface.getCurrentNetwork()
        return self._wpa_network_manage.getNetworkSSID(network)


    # Device state information
    def _get_device_ip(self):
        ip_pattern = "[0-9]+.[0-9]+.[0-9]+.[0-9]+"
        data = self._launch("ifconfig {}".format(self.interface))
        return re.search("inet addr:{}".format(ip_pattern), data).group(0)[10:]

    def _get_device_mac(self):
        mac_pattern = "..:..:..:..:..:.."
        data = self._launch("ifconfig {}".format(self.interface))
        return re.search(mac_pattern, data).group(0)

    def _get_state(self):
        if self._wpa_supplicant_start:
            return "wpa_supplicant"
        if self._hostapd_start:
            return "hostapd"
        return "wifi_off"

    # Connection actions
    def _check_correct_connection(self, any_network_flag):
        if any_network_flag:
            if self._get_current_network_ssid() != aim_network['ssid']:
                return False
        return True

    def _start_network_connection(self, network, any_network_flag):
        if not any_network:
            self._wpa_supplicant_interface.selectNetwork(network)
        else:
            self._wpa_supplicant_interface.reassociate()

    def _connect_to_network(self, network, any_network_flag):
        self._start_network_connection()
        try:
            self._wait_untill_connection_complete()
        except ConnectionError:
            return False
        else:
            return self._check_correct_connection(network, any_network_flag)

    def _choose_thread(self, network, callback=None, args=None, any_network=False):
        if callback is not None:
            self._connection_thread = Thread(target=self.connect, 
                args=(network, callback, any_network, args))
        else:
            self._connection_thread = Thread(target=self.connect, 
                args=(network, self._revert_on_connect_failure, any_network, network_state))

    def _start_connecting_thread(self):
        self._connection_timer = Timer(timeout, self.stop_connecting)
        self._connection_event.set()
        self._connection_thread.start()
        self._connection_timer.start()

    def _teardown_connection(self):
        self._connection_thread = None
        self._stop_timer_thread()
        if self._break_event.is_set():
            callback = None
            self._break_event.clear()

    def _stop_timer_thread(self):
        try:
            self._connection_timer.cancel()
        except AttributeError:
            pass

    def _break_connecting(self):
        if self._connection_thread is not None:
            self._break_event.set()
            try:
                if self._connection_timer.isAlive():
                    self._connection_timer.cancel()
                self._connection_timer = None
                self.stop_connecting()
            except AttributeError:
                pass

    def _wait_untill_connection_complete(self):
        while self._wpa_supplicant_interface.getState() != "completed":
            if not self._connection_event.is_set():
                raise ConnectionError


    # Callback
    def _revert_on_connect_failure(self, result, network_state):
        if not result:
            self.start_host_mode()

    # Subprocess
    def _launch(self, args):
        out_return = subprocess.check_output(
            args, stderr=subprocess.PIPE, shell=True)
        return out_return

if __name__ == '__main__':
    wifi = WiFiControl('wlp6s0')
    print(wifi.get_status())
