"""
Frontend E2E, step 2. Serve docs/ on :8765 (python -m http.server 8765 -d docs), then run.
Drive the real page like a user: connect, faucet, bet with permit, collect winnings, collect refund.
window.ethereum is a minimal EIP-1193 shim that forwards to the local node, which holds the
user's key, so signTypedData and sendTransaction behave like MetaMask after the user clicks OK.
"""
import asyncio, json, os
from pathlib import Path
from playwright.async_api import async_playwright
from web3 import Web3

HERE = Path(__file__).resolve().parent
env = json.loads((HERE / "env.json").read_text())
w3 = Web3(Web3.HTTPProvider("http://localhost:8545"))
ART = json.loads((HERE.parents[1] / "tests/artifacts/compiled.json").read_text())
token = w3.eth.contract(address=env["token"], abi=ART["MatchToken"]["abi"])
pool = w3.eth.contract(address=env["pool"], abi=ART["BettingPool"]["abi"])
E = 10**18
bal = lambda: token.functions.balanceOf(env["user"]).call() / E

INIT = """
window.__CONFIG_OVERRIDE__ = %s;
window.__alerts = 0; window.alert = () => { window.__alerts++; };
const RPC = "http://localhost:8545", USER = "%s";
const listeners = {};
window.ethereum = {
  isMetaMask: true,
  async request({ method, params = [] }) {
    if (method === "eth_requestAccounts" || method === "eth_accounts") return window.__connected || method === "eth_requestAccounts" ? (window.__connected = true, [USER]) : [];
    const r = await fetch(RPC, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: Date.now(), method, params }) }).then((x) => x.json());
    if (r.error) { const e = new Error(r.error.message); e.code = r.error.code; e.data = r.error.data; throw e; }
    return r.result;
  },
  on(ev, fn) { (listeners[ev] ||= []).push(fn); },
};
""" % (json.dumps({"rpc": "http://localhost:8545", "token": env["token"], "pool": env["pool"], "faucet": env["faucet"]}), env["user"])


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=os.environ.get("CHROME") or None, args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": 1280, "height": 1000})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        await pg.add_init_script(INIT)
        await pg.goto("http://localhost:8765/index.html?board=test_board.json")
        await pg.wait_for_selector(".rink")
        await pg.wait_for_timeout(1500)

        # sections
        print("open:", await pg.locator("#open .game").count(),
              "done:", await pg.locator("#done .game").count())
        # XSS probe rendered as text, never executed
        assert await pg.evaluate("window.__alerts") == 0
        assert await pg.locator("#done img[src='x']").count() == 0
        print("PASS xss probe shown as text, not executed")

        # connect
        await pg.get_by_role("button", name="Connect MetaMask").first.click()
        await pg.wait_for_selector(".wallet .bal")
        print("PASS connected, balance shown:", await pg.locator(".wallet .bal").inner_text())

        # faucet
        await pg.get_by_role("button", name="Get 100 free MATCH").click()
        await pg.wait_for_selector(".toast:has-text('Free tokens: done')", timeout=20000)
        assert bal() == 110, bal()
        await pg.wait_for_selector(".wallet:has-text('Next free tokens in')")
        print("PASS faucet: +100 MATCH, cooldown shown")

        # bet: pick away team on the open game, type 30, bet via permit
        await pg.get_by_role("button", name="Bet on HC Kometa Brno").click()
        inp = pg.locator(f"#amt-{env['mid_open']}")
        await inp.fill("30")
        est = await pg.locator(".estimate").inner_text()
        print("estimate:", est)
        await pg.screenshot(path=str(HERE / "ticket.png"))
        await pg.get_by_role("button", name="Bet 30 MATCH on HC Kometa Brno").click()
        await pg.wait_for_selector(".toast:has-text('Bet: done')", timeout=20000)
        stake = pool.functions.stakesOf(bytes.fromhex(env["mid_open"][2:]), env["user"]).call()
        assert stake == [0, 30 * E], stake
        assert bal() == 80, bal()
        print("PASS bet with permit: 30 MATCH on away, single tx (no approve)")

        # collect winnings and refund
        for label, expected in [("Collect 98 MATCH", 178), ("Collect 10 MATCH", 188)]:
            await pg.get_by_role("button", name=label).click()
            await pg.wait_for_selector(".toast:has-text('Collect: done')", timeout=20000)
            await pg.wait_for_function("document.querySelector('.toast').hidden || !document.querySelector('.toast').textContent.includes('Collect: done')", timeout=8000)
            assert bal() == expected, (label, bal())
            print(f"PASS {label}: balance {bal()}")
        assert await pg.get_by_role("button", name="Collect 98 MATCH").count() == 0

        # error path: betting more than the balance is blocked in the UI
        await pg.get_by_role("button", name="Bet on HC Sparta Praha").click()
        await pg.locator(f"#amt-{env['mid_open']}").fill("5000")
        assert await pg.get_by_role("button", name="Bet 5,000 MATCH on HC Sparta Praha").is_disabled()
        print("PASS over-balance bet disabled")

        await pg.screenshot(path=str(HERE / "after.png"), full_page=True)
        print("page errors:", errs or "none")
        await b.close()

asyncio.run(main())
