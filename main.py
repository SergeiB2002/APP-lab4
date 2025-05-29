import asyncio
import json
import random
import uuid
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, PeriodicBehaviour, OneShotBehaviour
from spade.message import Message

DOMAIN = "localhost"
MANAGER_JID = f"manager@{DOMAIN}"
MANAGER_PWD = "managerpwd"
STORAGE_JID = f"storage@{DOMAIN}"
STORAGE_PWD = "storagepwd"

# Параметр задержки обработки заказа (секунд на товар)
PROCESSING_TIME_PER_ITEM = 2

class ManagerAgent(Agent):
    def __init__(self, jid, password):
        super().__init__(jid, password)
        self.orders = []
        self.workers = []
        self.idle_workers = []

    class GenerateOrdersBehaviour(PeriodicBehaviour):
        async def run(self):
            n = random.randint(1, 10)
            items = [(f"item{random.randint(1,5)}", random.randint(1, 10)) for _ in range(n)]
            order_id = str(uuid.uuid4())
            self.agent.orders.append((order_id, items))
            print(f"[Manager] Generated order {order_id}: {items}")

    class DispatchBehaviour(CyclicBehaviour):
        async def run(self):
            if self.agent.orders:
                if not self.agent.idle_workers:
                    new_id = len(self.agent.workers) + 1
                    worker_jid = f"worker{new_id}@{DOMAIN}"
                    worker_pwd = "workerpwd"
                    worker = WorkerAgent(worker_jid, worker_pwd, self.agent.jid)
                    await worker.start(auto_register=True)
                    self.agent.workers.append(worker_jid)
                    self.agent.idle_workers.append(worker_jid)
                    print(f"[Manager] Spawned new worker {worker_jid}")
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
            if not msg:
                return
            mtype = msg.metadata.get("type")
            content = json.loads(msg.body) if msg.body else {}

            if mtype == "Report":
                print(f"[Manager] Received report for order {content.get('order_id')}: {content.get('status')}")
                self.agent.idle_workers.append(str(msg.sender))
            elif mtype == "OutOfStock":
                print(f"[Manager] Out of stock: {content.get('item')} qty {content.get('qty')}")
                self.agent.idle_workers.append(str(msg.sender))
                # Повторная попытка позже
                failed_order = content.get('order_id')
                # self.agent.orders.insert(0, (failed_order, self.agent.stored_items[failed_order]))
            elif mtype == "ReplenishNeeded":
                item = content.get('item')
                print(f"[Manager] Replenish needed for {item}")
                req = Message(to=STORAGE_JID)
                req.set_metadata("performative", "inform")
                req.set_metadata("type", "ReplenishRequest")
                req.body = json.dumps({"item": item, "amount": 100})
                await self.send(req)

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

    class NotifyReadyBehaviour(OneShotBehaviour):
        async def run(self):
            ready = Message(to=self.agent.manager_jid)
            ready.set_metadata("performative", "inform")
            ready.set_metadata("type", "Ready")
            await self.send(ready)

    class NewOrderBehaviour(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=10)
            if not msg or msg.metadata.get("type") != "NewOrder":
                return
            content = json.loads(msg.body)
            order_id = content["order_id"]
            items = content["items"]
            print(f"[{self.agent.jid}] Received order {order_id}: {items}")

            # Симуляция времени обработки заказа
            processing_time = len(items) * PROCESSING_TIME_PER_ITEM
            print(f"[{self.agent.jid}] Processing time: {processing_time}s")
            await asyncio.sleep(processing_time)

            for item, qty in items:
                req = Message(to=self.agent.storage_jid)
                req.set_metadata("performative", "request")
                req.set_metadata("type", "InventoryCheck")
                req.body = json.dumps({"item": item, "qty": qty})
                await self.send(req)
                reply = await self.receive(timeout=10)
                if reply and reply.metadata.get("type") == "InventoryInfo":
                    update = Message(to=self.agent.storage_jid)
                    update.set_metadata("performative", "inform")
                    update.set_metadata("type", "UpdateStock")
                    update.body = json.dumps({"item": item, "qty": qty})
                    await self.send(update)
                else:
                    out = Message(to=self.agent.manager_jid)
                    out.set_metadata("performative", "inform")
                    out.set_metadata("type", "OutOfStock")
                    out.body = json.dumps({"order_id": order_id, "item": item, "qty": qty})
                    await self.send(out)
                    return

            report = Message(to=self.agent.manager_jid)
            report.set_metadata("performative", "inform")
            report.set_metadata("type", "Report")
            report.body = json.dumps({"order_id": order_id, "status": "completed"})
            await self.send(report)

    async def setup(self):
        print(f"[{self.jid}] Worker starting...")
        self.add_behaviour(self.NotifyReadyBehaviour())
        self.add_behaviour(self.NewOrderBehaviour())

class StorageAgent(Agent):
    class InventoryBehaviour(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=10)
            if not msg:
                return
            mtype = msg.metadata.get("type")
            content = json.loads(msg.body) if msg.body else {}

            if mtype == "InventoryCheck":
                item = content.get("item")
                qty = content.get("qty")
                (row, shelf), rem = self.agent.inventory.get(item, ((None, None), 0))
                if rem >= qty:
                    info = Message(to=str(msg.sender))
                    info.set_metadata("performative", "inform")
                    info.set_metadata("type", "InventoryInfo")
                    info.body = json.dumps({"item": item, "location": (row, shelf), "remaining": rem - qty})
                    await self.send(info)
                    self.agent.inventory[item] = ((row, shelf), rem - qty)
                else:
                    notify = Message(to=self.agent.manager_jid)
                    notify.set_metadata("performative", "inform")
                    notify.set_metadata("type", "ReplenishNeeded")
                    notify.body = json.dumps({"item": item})
                    await self.send(notify)
                    out = Message(to=str(msg.sender))
                    out.set_metadata("performative", "inform")
                    out.set_metadata("type", "OutOfStock")
                    await self.send(out)
            elif mtype == "UpdateStock":
                item = content.get("item")
                qty = content.get("qty")
                (row, shelf), rem = self.agent.inventory.get(item, ((None, None), 0))
                self.agent.inventory[item] = ((row, shelf), rem - qty)
            elif mtype == "ReplenishRequest":
                item = content.get("item")
                amount = content.get("amount", 0)
                (row, shelf), rem = self.agent.inventory.get(item, ((None, None), 0))
                self.agent.inventory[item] = ((row, shelf), rem + amount)
                print(f"[Storage] Replenished {item} by {amount}, new remaining {rem + amount}")

    async def setup(self):
        print("[Storage] Agent starting...")
        self.inventory = {f"item{i}": ((i, i), 100) for i in range(1, 6)}
        self.manager_jid = MANAGER_JID
        self.add_behaviour(self.InventoryBehaviour())

async def main():
    storage = StorageAgent(STORAGE_JID, STORAGE_PWD)
    manager = ManagerAgent(MANAGER_JID, MANAGER_PWD)

    await storage.start(auto_register=True)
    await manager.start(auto_register=True)
    print("Agents started. Press Ctrl+C to stop...")

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("Stopping agents...")
        await manager.stop()
        await storage.stop()

if __name__ == "__main__":
    asyncio.run(main())
