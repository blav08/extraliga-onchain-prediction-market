// Compile the contracts and refresh the artifacts the oracle and tests use.
// Usage (from repo root):  npm i solc@0.8.24 @openzeppelin/contracts@5 && node scripts/compile.js
const solc = require("solc");
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const files = ["MatchToken.sol", "BettingPool.sol", "MatchFaucet.sol"];

const sources = {};
for (const f of files) sources[f] = { content: fs.readFileSync(path.join(root, "contracts", f), "utf8") };

const input = {
  language: "Solidity",
  sources,
  settings: {
    optimizer: { enabled: true, runs: 200 },
    evmVersion: "cancun",
    outputSelection: { "*": { "*": ["abi", "evm.bytecode.object"] } },
  },
};

// Resolve @openzeppelin/... imports from node_modules
const findImports = (p) => {
  try {
    return { contents: fs.readFileSync(path.join(root, "node_modules", p), "utf8") };
  } catch {
    return { error: "not found: " + p };
  }
};

const out = JSON.parse(solc.compile(JSON.stringify(input), { import: findImports }));
const errors = (out.errors || []).filter((e) => e.severity === "error");
(out.errors || []).forEach((e) => console.log(e.severity, e.formattedMessage.split("\n")[0]));
if (errors.length) process.exit(1);

const artifacts = {};
for (const f of files) {
  for (const [name, c] of Object.entries(out.contracts[f])) {
    artifacts[name] = { abi: c.abi, bytecode: c.evm.bytecode.object };
  }
}
fs.writeFileSync(path.join(root, "tests/artifacts/compiled.json"), JSON.stringify(artifacts));
fs.writeFileSync(path.join(root, "abi/BettingPool.json"), JSON.stringify(artifacts.BettingPool.abi, null, 1));
console.log("compiled:", Object.keys(artifacts).join(", "));
