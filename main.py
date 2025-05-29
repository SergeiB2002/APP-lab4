import asyncio
import json
import random
import uuid
import time
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, PeriodicBehaviour
from spade.message import Message

DOMAIN = "localhost"
MANAGER_JID = f"manager@{DOMAIN}"
MANAGER_PWD = "managerpwd"
STORAGE_JID = f"storage@{DOMAIN}"
STORAGE_PWD = "storagepwd"

class ManagerAgent(Agent):
    def __init__(self, jid, password):
        super().__init__(jid, password)
        self.orders = []
        self.workers = []
        self.idle_workers = []

    class GenerateOrdersBehaviour(PeriodicBehaviour):
        async def run(self):
            # Generate a random order
            n = random.randint(1, 10)
            items = [(f"item{random.randint(1,5)}", random.randint(1, 10)) for _ in range(n)]
            order_id = str(uuid.uuid4())
            self.agent.orders.append((order_id, items))
            print(f"[Manager] Generated order {order_id}: {items}")

    class DispatchBehaviour(CyclicBehaviour):
        async def run(self):
            if self.agent.orders:
                if not self.agent.idle_workers:
                    # Spawn a new worker
                    new_worker_num = len(self.agent.workers) + 1
                    worker_jid = f"worker{new_worker_num}@{DOMAIN}"
                    worker_pwd = "workerpwd"
                    worker = WorkerAgent(worker_jid, worker_pwd, self.agent.jid)
                    await worker.start(auto_register=True)
                    self.agent.workers.append(worker_jid)
                    self.agent.idle_workers.append(worker_jid)
                    print(f"[Manager] Spawned new worker {worker_jid}")
                # Dispatch next order
                order_id, items = self.agent.orders.pop(0)
                worker_jid = self.agent.idle_workers.pop(0)
                msg = Message(to=worker_jid)
                msg.set_metadata("performative", "inform")
                msg.set_metadata("type", "NewOrder")
                msg.body = json.dumps({"order_id": order_id, "items": items})
                await self.send(msg)
                print(f"[Manager] Dispatched order {order_id} to {worker_jid}")
            await asyncio.sleep(1)

    class ReportHandlerBehaviour(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=5)
            if msg:
                msg_type = msg.metadata.get("type")
                if msg_type == "Report":
                    content = json.loads(msg.body)
                    print(f"[Manager] Received report for order {content['order_id']}: {content['status']}")
                    self.agent.idle_workers.append(str(msg.sender))
                elif msg_type == "OutOfStock":
                    content = json.loads(msg.body)
                    print(f"[Manager] Out of stock: {content['item']} qty {content['qty']}")
                elif msg_type == "ReplenishNeeded":
                    content = json.loads(msg.body)
                    print(f"[Manager] Replenish needed for {content['item']}")

    async def setup(self):
        print("[Manager] Agent starting...")
        self.add_behaviour(self.GenerateOrdersBehaviour(period=10))
        self.add_behaviour(self.DispatchBehaviour())
        self.add_behaviour(self.ReportHandlerBehaviour())

class WorkerAgent(Agent):
    def __init__(self, jid, password, manager_jid):
        super().__init__(jid, password)
        self.manager_jid = manager_jid
        self.storage_jid = STORAGE_JID

    class NewOrderBehaviour(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=10)
            if msg and msg.metadata.get("type") == "NewOrder":
                content = json.loads(msg.body)
                order_id = content["order_id"]
                items = content["items"]
                print(f"[{str(self.agent.jid)}] Received order {order_id}: {items}")
                # Process each item
                for item, qty in items:
                    # Request inventory check
                    req = Message(to=self.agent.storage_jid)
                    req.set_metadata("performative", "request")
                    req.set_metadata("type", "InventoryCheck")
                    req.body = json.dumps({"item": item, "qty": qty})
                    await self.send(req)
                    # Wait for response
                    reply = await self.receive(lambda m: m.metadata.get("type") in ["InventoryInfo", "OutOfStock"], timeout=10)
                    if reply and reply.metadata.get("type") == "InventoryInfo":
                        # Confirm stock update
                        update = Message(to=self.agent.storage_jid)
                        update.set_metadata("performative", "inform")
                        update.set_metadata("type", "UpdateStock")
                        update.body = json.dumps({"item": item, "qty": qty})
                        await self.send(update)
                    else:
                        # Notify manager about out of stock
                        out = Message(to=self.agent.manager_jid)
                        out.set_metadata("performative", "inform")
                        out.set_metadata("type", "OutOfStock")
                        out.body = json.dumps({"order_id": order_id, "item": item, "qty": qty})
                        await self.send(out)
                        return
                # All items processed
                report = Message(to=self.agent.manager_jid)
                report.set_metadata("performative", "inform")
                report.set_metadata("type", "Report")
                report.body = json.dumps({"order_id": order_id, "status": "completed"})
                await self.send(report)
                # Notify manager worker is ready
                ready = Message(to=self.agent.manager_jid)
                ready.set_metadata("performative", "inform")
                ready.set_metadata("type", "Ready")
                await self.send(ready)

    async def setup(self):
        print(f"[{self.jid}] Worker starting, notifying manager...")
        # Notify manager this worker is ready
        ready = Message(to=self.manager_jid)
        ready.set_metadata("performative", "inform")
        ready.set_metadata("type", "Ready")
        await self.send(ready)
        self.add_behaviour(self.NewOrderBehaviour())

class StorageAgent(Agent):
    class InventoryBehaviour(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=10)
            if msg and msg.metadata.get("type") == "InventoryCheck":
                content = json.loads(msg.body)
                item = content["item"]
                qty = content["qty"]
                loc, rem = self.agent.inventory.get(item, ((None,None), 0))
                if rem >= qty:
                    # Send inventory info
                    info = Message(to=str(msg.sender))
                    info.set_metadata("performative", "inform")
                    info.set_metadata("type", "InventoryInfo")
                    info.body = json.dumps({"item": item, "location": loc, "remaining": rem-qty})
                    await self.send(info)
                    # Update inventory
                    self.agent.inventory[item] = (loc, rem-qty)
                else:
                    # Notify manager about replenish
                    notify = Message(to=self.agent.manager_jid)
                    notify.set_metadata("performative", "inform")
                    notify.set_metadata("type", "ReplenishNeeded")
                    notify.body = json.dumps({"item": item})
                    await self.send(notify)
                    # Inform worker
                    out = Message(to=str(msg.sender))
                    out.set_metadata("performative", "inform")
                    out.set_metadata("type", "OutOfStock")
                    await self.send(out)
            elif msg and msg.metadata.get("type") == "UpdateStock":
                content = json.loads(msg.body)
                item = content["item"]
                qty = content["qty"]
                loc, rem = self.agent.inventory.get(item, ((None,None), 0))
                self.agent.inventory[item] = (loc, rem-qty)

    async def setup(self):
        print("[Storage] Agent starting...")
        # Initial inventory: 5 items with 100 units each
        self.inventory = {f"item{i}": ((i, i), 100) for i in range(1, 6)}
        self.manager_jid = MANAGER_JID
        self.add_behaviour(self.InventoryBehaviour())

if __name__ == "__main__":
    # Start agents
    storage = StorageAgent(STORAGE_JID, STORAGE_PWD)
    manager = ManagerAgent(MANAGER_JID, MANAGER_PWD)

    future_storage = storage.start(auto_register=True)
    future_manager = manager.start(auto_register=True)

    future_storage.result()
    future_manager.result()

    print("Agents started. Press Ctrl+C to stop...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping agents...")
    manager.stop()
    storage.stop()
