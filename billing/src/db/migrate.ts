import { loadEnv } from "../config/env.js";
import { openDatabase } from "./client.js";

const env = loadEnv();
const database = openDatabase(env.DATABASE_URL);
await database.migrate();
await database.close();
console.log("migrations applied");
