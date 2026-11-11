#!/usr/bin/env node

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  input += chunk;
});
process.stdin.on("end", () => {
  try {
    const request = JSON.parse(input);
    if (
      request.schemaVersion !== "1.0" ||
      request.task !== "a_share_morning_context"
    ) {
      throw new Error("unsupported request");
    }
    const response = {
      schemaVersion: "1.0",
      meta: {
        status: "demo",
        provider: "mock-provider",
        sourceType: "stdio-command",
        targetDate: request.targetDate,
        asOf: new Date().toISOString(),
        timezone: request.timezone,
        isLive: false,
        warnings: ["Mock provider 只验证桥接协议，不提供真实金融数据。"]
      },
      data: {
        tradingCalendar: null,
        overnightGlobal: [],
        previousAShareSession: null,
        policyAndCalendar: [],
        sectors: [],
        capitalFlows: [],
        watchlist: [],
        announcementsAndNews: []
      },
      sources: []
    };
    process.stdout.write(JSON.stringify(response));
  } catch (error) {
    process.stderr.write("mock provider error: " + error.message + "\n");
    process.exitCode = 1;
  }
});

