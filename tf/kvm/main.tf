# Private network for inter-node communication
resource "openstack_networking_network_v2" "private_net" {
  name                  = "private-net-${var.suffix}"
  port_security_enabled = false
}

resource "openstack_networking_subnet_v2" "private_subnet" {
  name       = "private-subnet-${var.suffix}"
  network_id = openstack_networking_network_v2.private_net.id
  cidr       = "192.168.1.0/24"
  no_gateway = true
}

# Ports on private network — fixed IPs, no port security
resource "openstack_networking_port_v2" "private_net_ports" {
  for_each              = var.nodes
  name                  = "port-${each.key}-${var.suffix}"
  network_id            = openstack_networking_network_v2.private_net.id
  port_security_enabled = false

  fixed_ip {
    subnet_id  = openstack_networking_subnet_v2.private_subnet.id
    ip_address = each.value
  }
}

# Ports on sharednet1 — with security groups for external access
resource "openstack_networking_port_v2" "sharednet1_ports" {
  for_each   = var.nodes
  name       = "sharednet1-${each.key}-${var.suffix}"
  network_id = data.openstack_networking_network_v2.sharednet1.id
  security_group_ids = [
    data.openstack_networking_secgroup_v2.allow_ssh.id,
    data.openstack_networking_secgroup_v2.allow_http_80.id,
    data.openstack_networking_secgroup_v2.allow_2342.id,
    data.openstack_networking_secgroup_v2.allow_8000.id,
    data.openstack_networking_secgroup_v2.allow_8080.id,
    data.openstack_networking_secgroup_v2.allow_9001.id,
    data.openstack_networking_secgroup_v2.allow_6333.id,
    data.openstack_networking_secgroup_v2.allow_30234.id,
    data.openstack_networking_secgroup_v2.allow_30500.id,
    data.openstack_networking_secgroup_v2.allow_30633.id,
    data.openstack_networking_secgroup_v2.allow_30443.id,
    data.openstack_networking_secgroup_v2.allow_30300.id,
    data.openstack_networking_secgroup_v2.allow_30810.id,
  ]
}

# Compute instances
resource "openstack_compute_instance_v2" "nodes" {
  for_each = var.nodes

  name       = "${each.key}-${var.suffix}"
  image_name = "CC-Ubuntu24.04"
  flavor_id  = var.reservation
  key_pair   = var.key

  network {
    port = openstack_networking_port_v2.sharednet1_ports[each.key].id
  }

  network {
    port = openstack_networking_port_v2.private_net_ports[each.key].id
  }

  user_data = <<-EOF
    #! /bin/bash
    sudo echo "127.0.1.1 ${each.key}-${var.suffix}" >> /etc/hosts
    su cc -c /usr/local/bin/cc-load-public-keys
  EOF
}

# Floating IP — assigned to node1 only (jump host)
resource "openstack_networking_floatingip_v2" "floating_ip" {
  pool        = "public"
  description = "PhotoPrism IP for ${var.suffix}"
  port_id     = openstack_networking_port_v2.sharednet1_ports["node1"].id
}
