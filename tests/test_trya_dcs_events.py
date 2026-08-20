import asyncio
import unittest

from bot.trya_dcs_events import TryaDcsEventBroker


class TryaDcsEventBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_count_events_follow_subscriber_lifecycle(self):
        broker = TryaDcsEventBroker()

        async with broker.subscribe() as first:
            joined = await asyncio.wait_for(first.get(), timeout=0.2)
            self.assertEqual(joined["type"], "radio.listener_count")
            self.assertEqual(joined["data"]["count"], 1)

            async with broker.subscribe() as second:
                first_update = await asyncio.wait_for(first.get(), timeout=0.2)
                second_update = await asyncio.wait_for(second.get(), timeout=0.2)
                self.assertEqual(first_update["data"]["count"], 2)
                self.assertEqual(second_update["data"]["count"], 2)

            left = await asyncio.wait_for(first.get(), timeout=0.2)
            self.assertEqual(left["data"]["count"], 1)

        self.assertEqual(broker.listener_count, 0)

    async def test_slow_subscriber_keeps_latest_bounded_events(self):
        broker = TryaDcsEventBroker()

        async with broker.subscribe() as queue:
            await queue.get()  # Initial listener count event.
            for index in range(250):
                await broker.publish("radio.progress", {"index": index})

            self.assertEqual(queue.qsize(), 200)
            retained = [queue.get_nowait() for _ in range(queue.qsize())]
            self.assertEqual(retained[-1]["data"]["index"], 249)
            self.assertGreater(retained[0]["data"]["index"], 0)


if __name__ == "__main__":
    unittest.main()
