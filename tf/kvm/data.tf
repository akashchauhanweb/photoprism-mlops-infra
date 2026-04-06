data "openstack_networking_network_v2" "sharednet1" {
  name = "sharednet1"
}

data "openstack_networking_subnet_v2" "sharednet1_subnet" {
  name = "sharednet1-subnet"
}

data "openstack_networking_secgroup_v2" "allow_ssh" {
  name = "allow-ssh"
}

data "openstack_networking_secgroup_v2" "allow_http_80" {
  name = "allow-http-80"
}

data "openstack_networking_secgroup_v2" "allow_2342" {
  name = "allow-2342"
}

data "openstack_networking_secgroup_v2" "allow_8000" {
  name = "allow-8000"
}

data "openstack_networking_secgroup_v2" "allow_8080" {
  name = "allow-8080"
}

data "openstack_networking_secgroup_v2" "allow_9001" {
  name = "allow-9001"
}

data "openstack_networking_secgroup_v2" "allow_6333" {
  name = "allow-6333"
}

data "openstack_networking_secgroup_v2" "allow_30234" {
  name = "allow-30234"
}

data "openstack_networking_secgroup_v2" "allow_30500" {
  name = "allow-30500"
}

data "openstack_networking_secgroup_v2" "allow_30633" {
  name = "allow-30633-proj24"
}

data "openstack_networking_secgroup_v2" "allow_30443" {
  name = "allow-30443"
}

data "openstack_networking_secgroup_v2" "allow_30300" {
  name = "allow-30300"
}
