# SPDX-License-Identifier: Apache-2.0
#
# Investigation-item-1-4 PoC: confirms when/how LiteLLM's pre_call guardrail
# hook actually fires. There is no real judgment logic (no Argus judgment API
# call) — it only reproduces "block once a principal's call count crosses a
# threshold" to learn the hook's behavior hands-on.
#
# In the real Argus integration (2026-07-25 direction doc, job E), a call to
# the judgment API replaces this counting.

from collections import defaultdict

from fastapi import HTTPException
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy._types import UserAPIKeyAuth
from litellm.caching.caching import DualCache

BLOCK_AFTER = 3  # this principal is blocked from the 3rd call onward (a toy version of the Argus backstop)

# Cumulative call count per principal (= api_key). Resets on container
# restart — this is not a real persistence layer.
_call_counts: dict[str, int] = defaultdict(int)


class ArgusDummyGuardrail(CustomGuardrail):
    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: str,
    ):
        # --- This is where investigation item (c), "what identity info reaches the hook," was measured ---
        principal = user_api_key_dict.api_key or "unknown"
        team_id = getattr(user_api_key_dict, "team_id", None)
        user_id = getattr(user_api_key_dict, "user_id", None)
        key_alias = getattr(user_api_key_dict, "key_alias", None)

        _call_counts[principal] += 1
        count = _call_counts[principal]

        print(
            f"[argus-guardrail] pre_call principal={principal[:12]}... "
            f"team_id={team_id} user_id={user_id} key_alias={key_alias} "
            f"call_type={call_type} count={count}/{BLOCK_AFTER}",
            flush=True,
        )

        if count >= BLOCK_AFTER:
            print(f"[argus-guardrail] BLOCKING principal={principal[:12]}... (count={count})", flush=True)
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "argus_dummy_backstop",
                    "message": f"principal exceeded {BLOCK_AFTER} calls (dummy PoC, no real judgment)",
                },
            )

        return data
