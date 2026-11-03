# IBKR Paper integration test plan

Integration tests must run against an IBKR Paper session only.

Required cases:
1. Connect and wait for nextValidId before READY.
2. Disconnect -> no new order allocation.
3. Reconnect -> SYNCING first, then reconcile open orders/positions/executions.
4. Submit one tiny LIMIT order in a controlled symbol only after explicit test opt-in.
5. Capture openOrder, orderStatus and execDetails.
6. Restart worker while an order exists and prove no duplicate order is created.
7. Verify duplicate execId cannot create duplicate execution rows.

Never run these tests against a LIVE account.
