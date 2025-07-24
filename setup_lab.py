#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import subprocess
from jinja2 import Template

STATE_FILE = "hostonly_networks.json"

networks = [
    {"role": "internal", "subnet": "192.168.60.0/24",  "ip": "192.168.60.254", "netmask": "255.255.255.0"},
    {"role": "dmz1",     "subnet": "200.19.100.0/24",  "ip": "200.19.100.254", "netmask": "255.255.255.0"},
    {"role": "dmz2",     "subnet": "200.19.200.0/24",  "ip": "200.19.200.254", "netmask": "255.255.255.0"},
    {"role": "internet", "subnet": "100.18.190.0/24",  "ip": "100.18.190.254", "netmask": "255.255.255.0"}
]

def find_vboxmanage():
    env = os.getenv("VBOX_MANAGE_PATH")
    if env and os.path.isfile(env):
        return env
    if os.name == "nt":
        default = r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
        if os.path.isfile(default):
            return default
    return "VBoxManage"

vbm = find_vboxmanage()

def create_hostonly_networks():
    iface_map = {}
    print("Criando redes host-only no VirtualBox:")
    for net in networks:
        out = subprocess.check_output([vbm, "hostonlyif", "create"], text=True)
        name = re.search(r"'(.+)'", out).group(1)
        subprocess.check_call([
            vbm, "hostonlyif", "ipconfig", name,
            "--ip", net["ip"], "--netmask", net["netmask"]
        ])
        iface_map[net["role"]] = name
        print(f"  • {net['role']} → {name} ({net['subnet']})")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(iface_map, f, indent=2)
    print(f"\nNomes salvos em {STATE_FILE}")
    return iface_map

def load_hostonly_networks():
    if not os.path.isfile(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)

resp = input("Deseja criar as redes host-only? (s/N): ").strip().lower()
if resp == "s":
    iface_map = create_hostonly_networks()
else:
    iface_map = load_hostonly_networks()
    if not iface_map:
        iface_map = {}
        print("Informe manualmente os nomes das redes host-only existentes:")
        for net in networks:
            name = input(f"  {net['role']}: ").strip()
            iface_map[net["role"]] = name

vagrant_tpl = r'''
Vagrant.configure("2") do |config|
  # desabilita pasta padrão
  config.vm.synced_folder ".", "/vagrant", disabled: true
  # Disable automatic updates
  config.vbguest.auto_update = false
  # Alternatively, conditionally disable installation if the plugin is present
  if Vagrant.has_plugin?("vagrant-vbguest")
    config.vbguest.no_install = true
  end

  internal_net = "{{ internal }}"
  dmz1_net     = "{{ dmz1 }}"
  dmz2_net     = "{{ dmz2 }}"
  internet_net = "{{ internet }}"

  # 1) pfSense
  config.vm.define "pfsense" do |pf|
    pf.vm.box      = "kennyl/pfsense"
    pf.vm.hostname = "pfsense"
    pf.vm.provider "virtualbox" do |vb|
      vb.customize ["modifyvm", :id, "--nic1", "nat"]
      vb.customize ["modifyvm", :id, "--hostonlyadapter2", internet_net]
      vb.customize ["modifyvm", :id, "--hostonlyadapter3", internal_net]
      vb.customize ["modifyvm", :id, "--hostonlyadapter4", dmz1_net]
      vb.customize ["modifyvm", :id, "--hostonlyadapter5", dmz2_net]
      vb.name   = "pfsense"
      vb.memory = 1024
      vb.cpus   = 2
    end    

    pf.vm.network "private_network", adapter: 2,
                  ip:       "100.18.190.254", netmask: "255.255.255.0"

    pf.vm.network "private_network", adapter: 3,
                  ip:       "192.168.60.254", netmask: "255.255.255.0"

    pf.vm.network "private_network", adapter: 4,
                  ip:       "200.19.100.254", netmask: "255.255.255.0"

    pf.vm.network "private_network", adapter: 5,
                  ip:       "200.19.200.254", netmask: "255.255.255.0"

    pf.vm.network "forwarded_port", guest: 443, host: 8443, auto_correct: true

  end

  # 2) Cliente – GUI + hosts + rotas
  config.vm.define "cliente" do |c|
    c.vm.box      = "gusztavvargadr/xubuntu-desktop-2204-lts"
    c.vm.hostname = "cliente"
    c.vm.provider "virtualbox" do |vb|
      vb.gui = true
      vb.customize ["modifyvm", :id, "--nic1", "nat"]
      vb.customize ["modifyvm", :id, "--hostonlyadapter2", internal_net]
      vb.name   = "cliente"
      vb.memory = 1024
      vb.cpus   = 1
    end

    c.vm.network "private_network", adapter: 2,      
                  ip:       "192.168.60.10", netmask: "255.255.255.0"

    c.vm.provision "shell", run: "once", inline: <<-SHELL
      sudo apt-get update
      # hosts
      echo "200.19.200.10 internal.com"      | sudo tee -a /etc/hosts
      echo "100.18.190.10 external.com"      | sudo tee -a /etc/hosts
      echo "100.18.190.10 external-fake.com" | sudo tee -a /etc/hosts
      echo "200.19.100.10 internal-trap.com" | sudo tee -a /etc/hosts
    SHELL

    c.vm.provision "shell", run: "always", inline: <<-SHELL
      # rotas para outras sub-redes via pfSense internal
      sudo ip route add 200.19.100.0/24 via 192.168.60.254 dev enp0s8
      sudo ip route add 200.19.200.0/24 via 192.168.60.254 dev enp0s8
      sudo ip route add 100.18.190.0/24 via 192.168.60.254 dev enp0s8
    SHELL

  end

  # 3) honeypot – Docker + hosts + rotas
  config.vm.define "honeypot" do |h|
    h.vm.box      = "ubuntu/jammy64"
    h.vm.hostname = "honeypot"
    h.vm.provider "virtualbox" do |vb|
      vb.customize ["modifyvm", :id, "--nic1", "nat"]
      vb.customize ["modifyvm", :id, "--hostonlyadapter2", dmz1_net]
      vb.name   = "honeypot"
      vb.memory = 1024
      vb.cpus   = 2
    end

    h.vm.network "private_network", adapter: 2,
                  ip:       "200.19.100.10", netmask: "255.255.255.0"

    h.vm.provision "shell", run: "once", inline: <<-SHELL
      sudo apt-get update
      sudo apt-get install -y docker.io docker-compose
      sudo systemctl enable --now docker
      sudo usermod -aG docker vagrant
      # hosts
      echo "200.19.200.10 internal.com"      | sudo tee -a /etc/hosts
      echo "100.18.190.10 external.com"      | sudo tee -a /etc/hosts
      echo "100.18.190.10 external-fake.com" | sudo tee -a /etc/hosts
      echo "200.19.100.10 internal-trap.com" | sudo tee -a /etc/hosts
    SHELL

    h.vm.provision "shell", run: "always", inline: <<-SHELL
      # rotas via pfSense dmz1
      sudo ip route add 192.168.60.0/24  via 200.19.100.254 dev enp0s8
      sudo ip route add 200.19.200.0/24  via 200.19.100.254 dev enp0s8
      sudo ip route add 100.18.190.0/24  via 200.19.100.254 dev enp0s8
    SHELL

  end

  # 4) Internal Server – Nginx + FTP + hosts + rotas
  config.vm.define "internal-server" do |w|
    w.vm.box      = "ubuntu/jammy64"
    w.vm.hostname = "internal-server"
    w.vm.provider "virtualbox" do |vb|
      vb.customize ["modifyvm", :id, "--nic1", "nat"]
      vb.customize ["modifyvm", :id, "--hostonlyadapter2", dmz2_net]
      vb.name   = "internal-server"
      vb.memory = 1024
      vb.cpus   = 1
    end

    w.vm.network "private_network", adapter: 2,
                  ip:       "200.19.200.10", netmask: "255.255.255.0"

    w.vm.provision "shell", run: "once", inline: <<-SHELL
      sudo apt-get update
      sudo apt-get install -y nginx vsftpd
      cat <<EOF | sudo tee /var/www/html/index.html
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>internal.com</title></head>
<body style="font-family:Arial;text-align:center;padding:2em;">
  <h1>internal.com</h1>
</body></html>
EOF
      sudo systemctl enable --now nginx

      [ -f /etc/vsftpd.conf.orig ] || sudo cp /etc/vsftpd.conf /etc/vsftpd.conf.orig

      for param in \
        "listen=YES" \
        "anonymous_enable=NO" \
        "local_enable=YES" \
        "write_enable=YES" \
        "chroot_local_user=YES"
      do
        key=$(echo "$param" | cut -d= -f1)
        sudo sed -i "s/^#\?$key=.*/$param/" /etc/vsftpd.conf || \
        grep -q "^$param" /etc/vsftpd.conf || \
        echo "$param" | sudo tee -a /etc/vsftpd.conf
      done

      # Verifica se o usuário já existe
      if id aluno &>/dev/null; then
        echo "Usuario 'aluno' já existe, pulando criacao."
      else
        echo "Criando usuario 'aluno'..."
        sudo useradd -m aluno -s /usr/sbin/nologin
        echo "aluno:aluno" | sudo chpasswd
      fi

      # Ativa o serviço
      sudo systemctl enable --now vsftpd

      # hosts
      echo "200.19.200.10 internal.com"      | sudo tee -a /etc/hosts
      echo "100.18.190.10 external.com"      | sudo tee -a /etc/hosts
      echo "100.18.190.10 external-fake.com" | sudo tee -a /etc/hosts
      echo "200.19.100.10 internal-trap.com" | sudo tee -a /etc/hosts
    SHELL

    w.vm.provision "shell", run: "always", inline: <<-SHELL
      # rotas via pfSense dmz2
      sudo ip route add 192.168.60.0/24  via 200.19.200.254 dev enp0s8
      sudo ip route add 200.19.100.0/24  via 200.19.200.254 dev enp0s8
      sudo ip route add 100.18.190.0/24  via 200.19.200.254 dev enp0s8
    SHELL

  end

  # 5) External Server – Nginx + hosts + rotas (enp0s8)
  config.vm.define "external-server" do |k|
    k.vm.box      = "gusztavvargadr/xubuntu-desktop-2204-lts"
    k.vm.hostname = "external-server"
    k.vm.provider "virtualbox" do |vb|
      vb.customize ["modifyvm", :id, "--nic1", "nat"]
      vb.customize ["modifyvm", :id, "--hostonlyadapter2", internet_net]
      vb.name   = "external-server"
      vb.memory = 1024
      vb.cpus   = 1
      vb.gui    = true
    end

    k.vm.network "private_network", adapter: 2,
                  ip:       "100.18.190.10", netmask: "255.255.255.0"

    k.vm.provision "shell", run: "once", inline: <<-SHELL
      sudo apt-get update
      sudo apt-get install -y nginx

      # página external.com
      cat <<'EOF' | sudo tee /var/www/html/index.html
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>external.com</title>
  <style>
    body { background:#222; color:#fff; font-family:Verdana, sans-serif; text-align:center; padding:2em; }
    h1 { font-size:3em; }
  </style>
</head>
<body>
  <h1>external.com</h1>
  <p>Servidor Autorizado</p>
</body>
</html>
EOF

      # página external-fake.com
      cat <<'EOF' | sudo tee /var/www/html/fake.html
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>external-fake.com</title>
  <style>
    body { background:#faf0e6; color:#a00; font-family:'Courier New',monospace; text-align:center; padding:3em; }
    h1 { font-size:4em; }
    p  { font-size:1.3em; }
    .notice { margin-top:1.5em; color:#555; }
  </style>
</head>
<body>
  <h1>external-fake.com</h1>
  <p>Acesso negado a este domínio.</p>
  <div class="notice">Página de demonstração.</div>
</body>
</html>
EOF
      # cria arquivo de vhost
      cat <<'EOF' | sudo tee /etc/nginx/sites-available/external.conf
server {
    listen 80;
    server_name external.com;
    root /var/www/html;
    index index.html;
}

server {
    listen 80;
    server_name external-fake.com;
    root /var/www/html;
    index fake.html;
}
EOF

      # habilita e recarrega
      sudo ln -sf /etc/nginx/sites-available/external.conf /etc/nginx/sites-enabled/default
      sudo systemctl reload nginx

      # hosts
      echo "200.19.200.10 internal.com"      | sudo tee -a /etc/hosts
      echo "100.18.190.10 external.com"      | sudo tee -a /etc/hosts
      echo "100.18.190.10 external-fake.com" | sudo tee -a /etc/hosts
      echo "200.19.100.10 internal-trap.com" | sudo tee -a /etc/hosts      
    SHELL

    k.vm.provision "shell", run: "always", inline: <<-SHELL
      # rotas via pfSense wan (eth1)
      sudo ip route add 192.168.60.0/24  via 100.18.190.254 dev enp0s8
      sudo ip route add 200.19.100.0/24  via 100.18.190.254 dev enp0s8
      sudo ip route add 200.19.200.0/24  via 100.18.190.254 dev enp0s8
    SHELL

  end

end
'''

template = Template(vagrant_tpl)
rendered = template.render(
    internal=iface_map["internal"],
    dmz1=iface_map["dmz1"],
    dmz2=iface_map["dmz2"],
    internet=iface_map["internet"]
)

with open("Vagrantfile", "w", encoding="utf-8") as f:
    f.write(rendered)

print("✔️ Vagrantfile gerado com sucesso.")