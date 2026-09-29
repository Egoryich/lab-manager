"""Operator-run transport PKI. Private keys stay on their originating host."""

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import ssl
import subprocess
import uuid
from pathlib import Path


def openssl(*args):
    result = subprocess.run(
        ["openssl", *map(str, args)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("OpenSSL operation failed; existing files were preserved")
    return result.stdout


def fingerprint(path):
    der = ssl.PEM_cert_to_DER_cert(path.read_text(encoding="ascii"))
    return hashlib.sha256(der).hexdigest()


def write(path, contents):
    with path.open("x", encoding="ascii") as output:
        output.write(contents)


def initialize_node(directory):
    if any(directory.iterdir()):
        raise ValueError("Node TLS directory must be empty; never overwrite existing keys")
    node_id = str(uuid.uuid4())
    write(directory / "node-id", node_id + "\n")
    openssl(
        "genpkey",
        "-algorithm",
        "RSA",
        "-pkeyopt",
        "rsa_keygen_bits:3072",
        "-out",
        directory / "server.key",
    )
    openssl(
        "req",
        "-new",
        "-key",
        directory / "server.key",
        "-subj",
        f"/CN={node_id}",
        "-out",
        directory / "server.csr",
    )
    print("Node ID:", node_id)
    print((directory / "server.csr").read_text(encoding="ascii"))


def initialize_control(directory):
    if any(directory.iterdir()):
        raise ValueError("Control PKI directory must be empty; never overwrite existing keys")
    openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:3072",
        "-nodes",
        "-sha256",
        "-days",
        "1095",
        "-subj",
        "/CN=Lab Manager transport CA",
        "-keyout",
        directory / "ca.key",
        "-out",
        directory / "ca.pem",
        "-addext",
        "basicConstraints=critical,CA:TRUE,pathlen:0",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        "-addext",
        "subjectKeyIdentifier=hash",
    )
    openssl(
        "req",
        "-new",
        "-newkey",
        "rsa:3072",
        "-nodes",
        "-subj",
        "/CN=Lab Manager worker",
        "-keyout",
        directory / "client.key",
        "-out",
        directory / "client.csr",
    )
    write(
        directory / "client.ext",
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n",
    )
    openssl(
        "x509",
        "-req",
        "-in",
        directory / "client.csr",
        "-CA",
        directory / "ca.pem",
        "-CAkey",
        directory / "ca.key",
        "-set_serial",
        "0x" + uuid.uuid4().hex,
        "-days",
        "365",
        "-sha256",
        "-extfile",
        directory / "client.ext",
        "-out",
        directory / "client.pem",
    )
    print("Control PKI created. Private keys were not displayed.")


def sign_node(directory, csr, node_id, address):
    node_id = str(uuid.UUID(node_id))
    address = str(ipaddress.ip_address(address))
    destination = directory / node_id
    destination.mkdir(mode=0o700)
    openssl("req", "-in", csr, "-verify", "-noout")
    subject = openssl("req", "-in", csr, "-noout", "-subject", "-nameopt", "RFC2253").decode()
    if not re.search(rf"(?:^|[ ,=])CN={re.escape(node_id)}(?:,|$)", subject):
        raise ValueError("CSR node identity does not match the requested node ID")
    write(
        destination / "server.ext",
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
        f"subjectAltName=IP:{address}\nsubjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n",
    )
    openssl(
        "x509",
        "-req",
        "-in",
        csr,
        "-CA",
        directory / "ca.pem",
        "-CAkey",
        directory / "ca.key",
        "-set_serial",
        "0x" + uuid.uuid4().hex,
        "-days",
        "365",
        "-sha256",
        "-extfile",
        destination / "server.ext",
        "-out",
        destination / "server.pem",
    )
    bundle = {
        "node_id": node_id,
        "address": address,
        "ca_pem": (directory / "ca.pem").read_text(encoding="ascii"),
        "server_pem": (destination / "server.pem").read_text(encoding="ascii"),
        "server_sha256": fingerprint(destination / "server.pem"),
        "client_sha256": fingerprint(directory / "client.pem"),
    }
    write(destination / "public-bundle.json", json.dumps(bundle))
    print(base64.b64encode(json.dumps(bundle).encode()).decode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["node-init", "control-init", "sign-node"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--csr", type=Path)
    parser.add_argument("--node-id")
    parser.add_argument("--address")
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        parser.error("Run as root on the target Linux host")
    os.umask(0o077)
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action == "node-init":
        initialize_node(args.directory)
    elif args.action == "control-init":
        initialize_control(args.directory)
    else:
        if not all((args.csr, args.node_id, args.address)):
            parser.error("sign-node requires --csr, --node-id and --address")
        sign_node(args.directory, args.csr, args.node_id, args.address)


if __name__ == "__main__":
    main()
