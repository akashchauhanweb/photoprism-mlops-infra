variable "suffix" {
  description = "Suffix for resource names (project ID)"
  type        = string
  nullable    = false
}

variable "key" {
  description = "Name of SSH key pair on Chameleon"
  type        = string
  default     = "id_rsa_chameleon"
}

variable "reservation" {
  description = "UUID of the reserved flavor"
  type        = string
}

variable "nodes" {
  type = map(string)
  default = {
    "node1" = "192.168.1.11"
    "node2" = "192.168.1.12"
    "node3" = "192.168.1.13"
  }
}
