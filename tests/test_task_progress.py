import unittest

from services.task_progress import sse_progress_stream


class ProgressKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_keepalive_is_emitted_while_long_task_is_idle(self):
        progress = {"company": []}
        running = {"company"}
        stream = sse_progress_stream(
            "company",
            progress,
            running,
            lambda: None,
            max_ticks=1,
            interval=0,
            keepalive=True,
        )

        chunks = [chunk async for chunk in stream]

        self.assertEqual(chunks[0], ": keepalive\n\n")
        self.assertIn('"type": "done"', chunks[-1])


if __name__ == "__main__":
    unittest.main()
