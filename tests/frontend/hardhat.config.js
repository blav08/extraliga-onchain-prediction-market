// Local chain for the frontend E2E test. Same chain id as Sepolia, so the page needs no changes.
// Run from this folder:  npm i hardhat@2 && npx hardhat node
module.exports = { solidity: "0.8.24", networks: { hardhat: { chainId: 11155111, hardfork: "cancun" } } };
