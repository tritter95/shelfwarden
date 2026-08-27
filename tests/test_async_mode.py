"""pytest-asyncio is configured in `asyncio_mode = "auto"`; nothing proved it.

Without this test, an accidental removal of the setting would go unnoticed until
the first real async test -- in step 1.1 -- silently stopped running.
"""

import asyncio


async def test_async_tests_run_without_a_decorator():
    await asyncio.sleep(0)
    assert asyncio.get_running_loop().is_running()
