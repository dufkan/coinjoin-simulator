import btc_node
from wasabi_client import WasabiClient
from time import sleep
import random
import docker
import os
import shutil
import datetime

BTC = 100_000_000

docker_client = None
docker_network = None
distributor = None
clients = []


def build_images():
    print("Building Docker images")
    docker_client.images.build(path="../btc-node", tag="btc-node", rm=True)
    print("- btc-node image built")
    docker_client.images.build(path="../wasabi-backend", tag="wasabi-backend", rm=True)
    print("- wasabi-backend image built")
    docker_client.images.build(path="../wasabi-client", tag="wasabi-client", rm=True)
    print("- wasabi-client image built")


def start_infrastructure():
    print("Starting infrastructure")
    old_networks = docker_client.networks.list("coinjoin")
    if old_networks:
        for old_network in old_networks:
            print(f"- removing old CoinJoin network {old_network.id[:12]!r}")
            old_network.remove()

    global docker_network
    docker_network = docker_client.networks.create("coinjoin", driver="bridge")
    print(f"- created new CoinJoin network {docker_network.id[:12]!r}")

    docker_client.containers.run(
        "btc-node",
        detach=True,
        auto_remove=True,
        name="btc-node",
        hostname="btc-node",
        ports={"18443": "18443"},
        network=docker_network.id,
    )
    sleep(10)  # TODO perform health check instead
    print("- started btc-node")

    if os.path.exists("../mounts/backend/"):
        shutil.rmtree("../mounts/backend/")
    os.mkdir("../mounts/backend/")
    shutil.copyfile("../wasabi-backend/Config.json", "../mounts/backend/Config.json")
    shutil.copyfile(
        "../wasabi-backend/WabiSabiConfig.json",
        "../mounts/backend/WabiSabiConfig.json",
    )
    docker_client.containers.run(
        "wasabi-backend",
        detach=True,
        auto_remove=True,
        name="wasabi-backend",
        hostname="wasabi-backend",
        ports={"37127": "37127"},
        environment=["WASABI_BIND=http://0.0.0.0:37127"],
        volumes=[
            f"{os.path.abspath('../mounts/backend/')}:/home/wasabi/.walletwasabi/backend/"
        ],
        network=docker_network.id,
    )
    sleep(10)  # TODO perform health check instead
    print("- started wasabi-backend")

    docker_client.containers.run(
        "wasabi-client",
        detach=True,
        auto_remove=True,
        name=f"wasabi-client-distributor",
        hostname=f"wasabi-client-distributor",
        ports={"37128": "37128"},
        network=docker_network.id,
    )
    sleep(10)
    global distributor
    distributor = WasabiClient("wasabi-client-distributor", 37128)
    distributor.create_wallet()
    distributor.wait_wallet()
    print("- started distributor")


def fund_distributor(btc_amount):
    print("Funding distributor")
    btc_node.fund_address(distributor.get_new_address(), btc_amount)
    btc_node.mine_block()
    while (balance := distributor.get_balance()) < btc_amount * BTC:
        sleep(0.1)
    print(f"- funded (current balance {balance / BTC:.8f} BTC)")


def start_clients(num_clients):
    print("Starting clients")
    new_idxs = []
    for _ in range(num_clients):
        idx = len(clients)
        docker_client.containers.run(
            "wasabi-client",
            detach=True,
            auto_remove=True,
            name=f"wasabi-client-{idx}",
            hostname=f"wasabi-client-{idx}",
            ports={"37128": 37129 + idx},
            network=docker_network.id,
        )
        client = WasabiClient(f"wasabi-client-{idx}", 37129 + idx)
        clients.append(client)
        new_idxs.append(idx)
        print(f"- started {client.name}")
    sleep(10)  # TODO perform health check instead
    for idx in new_idxs:
        client = clients[idx]
        client.create_wallet()
        client.wait_wallet()
        print(f"- created wallet {client.name}")
    return new_idxs


def fund_clients(invoices):
    print("Funding clients")
    addressed_invoices = [
        (client.get_new_address(), amount) for client, amount in invoices
    ]
    distributor.send(addressed_invoices)
    print("- created wallet-funding transaction")
    btc_node.mine_block()
    for client, target_value in invoices:
        while client.get_balance() < target_value:
            sleep(0.1)
    print("- funded")


def start_coinjoins():
    print("Starting coinjoins")
    for client in clients:
        client.start_coinjoin()
        print(f"- started {client.name}")


def main():
    build_images()
    start_infrastructure()
    fund_distributor(30)
    idxs = start_clients(8)
    fund_clients(
        [(clients[idx], int(random.random() * BTC * 0.001 + BTC)) for idx in idxs]
    )
    start_coinjoins()

    print("Running")
    while True:
        with open("../mounts/backend/WabiSabi/CoinJoinIdStore.txt") as f:
            num_lines = sum(1 for _ in f)
        print(f"- number of coinjoins: {num_lines:<10}", end="\r")
        sleep(1)


if __name__ == "__main__":
    docker_client = docker.from_env()
    try:
        main()
    except KeyboardInterrupt:
        print("KeyboardInterrupt received")
    finally:
        print("Storing logs")
        if not os.path.exists("../logs/"):
            os.mkdir("../logs/")
        shutil.copytree(
            "../mounts/backend/",
            f"../logs/{datetime.datetime.now().isoformat(timespec='seconds')}/",
        )

        print("Stopping infrastructure")
        try:
            docker_client.containers.get("btc-node").stop()
        except docker.errors.NotFound:
            pass
        try:
            docker_client.containers.get("wasabi-backend").stop()
        except docker.errors.NotFound:
            pass
        try:
            docker_client.containers.get(distributor.name).stop()
        except docker.errors.NotFound:
            pass
        for client in clients:
            try:
                docker_client.containers.get(client.name).stop()
            except docker.errors.NotFound:
                pass