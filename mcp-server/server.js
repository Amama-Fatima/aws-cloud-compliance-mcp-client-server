const express = require("express");
const cors = require("cors");
const { spawn } = require("child_process");

const app = express();
const PORT = process.env.PORT || 10000;

// Configuration constants
const CONFIG = {
  REQUEST_TIMEOUT: parseInt(process.env.MCP_TOOL_TIMEOUT) || 180000, // 3 minutes
  SESSION_IDLE_TIMEOUT: parseInt(process.env.SESSION_IDLE_TIMEOUT) || 1800000, // 30 minutes
  SESSION_MAX_LIFETIME: parseInt(process.env.SESSION_MAX_LIFETIME) || 3600000, // 1 hour
  KEEPALIVE_INTERVAL: 15000,
  INITIALIZATION_TIMEOUT: 30000,
  SESSION_CHECK_INTERVAL: 60000,
};

// CORS for claude.ai
app.use(
  cors({
    origin: "*",
    methods: ["GET", "POST", "OPTIONS"],
    allowedHeaders: [
      "Content-Type",
      "Authorization",
      "Accept",
      "Cache-Control",
      "Last-Event-ID",
      "User-Agent",
      "Origin",
      "Referer",
      "Mcp-Session-Id",
      "MCP-Protocol-Version",
    ],
    exposedHeaders: ["Content-Type", "Mcp-Session-Id", "MCP-Protocol-Version"],
    credentials: false,
  })
);

app.use(express.json());

// Store active MCP processes per session
const activeSessions = new Map();

function createMcpProcess() {
  console.log("Creating new Java MCP process...");

  // Spawn the Java process
  const mcpProcess = spawn("java", ["-jar", "/app/cloud-compliance-mcp.jar"], {
    stdio: ["pipe", "pipe", "pipe"],
    env: {
      ...process.env,
      AWS_ACCESS_KEY_ID: process.env.AWS_ACCESS_KEY_ID,
      AWS_SECRET_ACCESS_KEY: process.env.AWS_SECRET_ACCESS_KEY,
      AWS_REGION: process.env.AWS_REGION || "ap-south-1",
    },
  });

  mcpProcess.on("error", (error) => {
    console.error("MCP process error:", error);
  });

  mcpProcess.on("exit", (code) => {
    console.log(`MCP process exited with code ${code}`);
  });

  return mcpProcess;
}

// Clean up a specific session
function cleanupSession(sessionId, reason = "timeout") {
  const sessionData = activeSessions.get(sessionId);
  if (!sessionData) return;

  console.log(`Cleaning up session ${sessionId} (reason: ${reason})`);

  if (sessionData.idleTimer) clearTimeout(sessionData.idleTimer);
  if (sessionData.lifetimeTimer) clearTimeout(sessionData.lifetimeTimer);
  if (sessionData.keepAliveInterval)
    clearInterval(sessionData.keepAliveInterval);

  sessionData.pendingRequests.forEach((request) => {
    if (request.timeout) clearTimeout(request.timeout);
    if (request.progressInterval) clearInterval(request.progressInterval);
    if (!request.res.headersSent) {
      request.res.status(408).json({
        jsonrpc: "2.0",
        id: null,
        error: {
          code: -32000,
          message: `Session terminated: ${reason}`,
        },
      });
    }
  });
  sessionData.pendingRequests.clear();

  if (sessionData.sseRes && !sessionData.sseRes.writableEnded) {
    sessionData.sseRes.end();
  }

  if (sessionData.process && !sessionData.process.killed) {
    sessionData.process.kill();
  }

  activeSessions.delete(sessionId);
}

// Reset idle timer
function resetSessionIdleTimer(sessionData, sessionId) {
  if (sessionData.idleTimer) {
    clearTimeout(sessionData.idleTimer);
  }

  sessionData.lastActivity = Date.now();
  sessionData.idleTimer = setTimeout(() => {
    cleanupSession(sessionId, "idle timeout");
  }, CONFIG.SESSION_IDLE_TIMEOUT);
}

// Periodic cleanup
setInterval(() => {
  const now = Date.now();
  activeSessions.forEach((sessionData, sessionId) => {
    if (now - sessionData.createdAt > CONFIG.SESSION_MAX_LIFETIME) {
      cleanupSession(sessionId, "max lifetime exceeded");
      return;
    }

    if (sessionData.process && sessionData.process.killed) {
      cleanupSession(sessionId, "process died");
      return;
    }

    if (now - sessionData.lastActivity > CONFIG.SESSION_IDLE_TIMEOUT) {
      cleanupSession(sessionId, "idle too long");
    }
  });
}, CONFIG.SESSION_CHECK_INTERVAL);

// Health check
app.get("/health", (req, res) => {
  const hasAWS = !!(
    process.env.AWS_ACCESS_KEY_ID && process.env.AWS_SECRET_ACCESS_KEY
  );

  res.json({
    status: "healthy",
    service: "cloud-compliance-mcp-server",
    timestamp: new Date().toISOString(),
    integrations: {
      aws: hasAWS ? "configured" : "missing",
    },
    config: {
      requestTimeout: `${CONFIG.REQUEST_TIMEOUT}ms`,
      sessionIdleTimeout: `${CONFIG.SESSION_IDLE_TIMEOUT}ms`,
      sessionMaxLifetime: `${CONFIG.SESSION_MAX_LIFETIME}ms`,
    },
    sessions: {
      active: activeSessions.size,
    },
  });
});

// Root endpoint
app.get("/", (req, res) => {
  res.json({
    service: "Cloud Compliance MCP Server",
    version: "1.0.0",
    transport: "Streamable HTTP",
    endpoints: {
      health: "/health",
      mcp: "/mcp",
    },
    documentation: "Connect your MCP client to /mcp endpoint",
  });
});

// Generate session ID
function generateSessionId(req) {
  const headerSessionId = req.get("Mcp-Session-Id");
  if (headerSessionId) return headerSessionId;

  const ip = req.ip || req.connection.remoteAddress || "unknown";
  const userAgent = req.get("user-agent") || "unknown";
  return Buffer.from(ip + userAgent)
    .toString("base64")
    .slice(0, 16);
}

// Get or create session
function getOrCreateSession(sessionId) {
  if (activeSessions.has(sessionId)) {
    const session = activeSessions.get(sessionId);
    resetSessionIdleTimer(session, sessionId);
    return session;
  }

  console.log(`Creating new session: ${sessionId}`);
  const mcpProcess = createMcpProcess();
  const now = Date.now();

  const sessionData = {
    process: mcpProcess,
    initialized: false,
    initializing: false,
    pendingRequests: new Map(),
    responseBuffer: "",
    listenersSetup: false,
    lastActivity: now,
    createdAt: now,
    sseRes: null,
    idleTimer: null,
    lifetimeTimer: null,
    keepAliveInterval: null,
  };

  resetSessionIdleTimer(sessionData, sessionId);

  sessionData.lifetimeTimer = setTimeout(() => {
    cleanupSession(sessionId, "max lifetime reached");
  }, CONFIG.SESSION_MAX_LIFETIME);

  activeSessions.set(sessionId, sessionData);
  return sessionData;
}

// SSE endpoint
app.get("/mcp", (req, res) => {
  const sessionId = generateSessionId(req);
  console.log(`SSE stream opened for session ${sessionId}`);

  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Expose-Headers": "Mcp-Session-Id, MCP-Protocol-Version",
    "Mcp-Session-Id": sessionId,
    "MCP-Protocol-Version": "2024-11-05",
  });

  res.write(`: Connected to session ${sessionId}\n\n`);

  const sessionData = getOrCreateSession(sessionId);
  sessionData.sseRes = res;

  const keepAlive = setInterval(() => {
    if (!res.writableEnded) {
      res.write(`: ping ${Date.now()}\n\n`);
    } else {
      clearInterval(keepAlive);
    }
  }, CONFIG.KEEPALIVE_INTERVAL);

  sessionData.keepAliveInterval = keepAlive;

  req.on("close", () => {
    clearInterval(keepAlive);
    sessionData.sseRes = null;
    console.log(`SSE stream closed for session ${sessionId}`);
  });
});

// Main MCP POST endpoint
app.post("/mcp", async (req, res) => {
  const message = req.body;
  const sessionId = generateSessionId(req);

  console.log("=== Received MCP request ===");
  console.log("Session:", sessionId);
  console.log("Method:", message.method);
  console.log("ID:", message.id);
  console.log("============================");

  res.setHeader("Mcp-Session-Id", sessionId);
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader(
    "Access-Control-Expose-Headers",
    "Mcp-Session-Id, MCP-Protocol-Version"
  );
  res.setHeader("MCP-Protocol-Version", "2024-11-05");

  if (!message.jsonrpc || !message.method) {
    return res.status(400).json({
      jsonrpc: "2.0",
      id: message.id || null,
      error: { code: -32600, message: "Invalid Request" },
    });
  }

  const sessionData = getOrCreateSession(sessionId);
  const mcpProcess = sessionData.process;

  if (!mcpProcess || mcpProcess.killed) {
    cleanupSession(sessionId, "process unavailable");
    return res.status(500).json({
      jsonrpc: "2.0",
      id: message.id || null,
      error: {
        code: -32603,
        message: "Internal error: MCP process not available",
      },
    });
  }

  if (message.method === "initialize") {
    if (sessionData.initialized) {
      sessionData.initialized = false;
    }
    sessionData.initializing = true;
  }

  try {
    // Handle notifications
    if (message.method.startsWith("notifications/")) {
      console.log(`Processing notification: ${message.method}`);
      mcpProcess.stdin.write(JSON.stringify(message) + "\n");

      if (message.method === "notifications/initialized") {
        sessionData.initialized = true;
        sessionData.initializing = false;
      }

      return res.status(202).json({ success: true });
    }

    // Determine timeout
    let timeoutDuration = CONFIG.REQUEST_TIMEOUT;
    if (message.method === "initialize") {
      timeoutDuration = CONFIG.INITIALIZATION_TIMEOUT;
    }

    // Progress interval for long-running operations
    let progressInterval = null;
    if (
      message.method === "tools/call" &&
      sessionData.sseRes &&
      !sessionData.sseRes.writableEnded
    ) {
      let progressCount = 0;
      progressInterval = setInterval(() => {
        if (sessionData.sseRes && !sessionData.sseRes.writableEnded) {
          progressCount++;
          sessionData.sseRes.write(`: progress ${progressCount}\n\n`);
        }
      }, 10000);
    }

    const responseTimeout = setTimeout(() => {
      if (progressInterval) clearInterval(progressInterval);

      if (
        message.id !== undefined &&
        sessionData.pendingRequests.has(message.id)
      ) {
        const request = sessionData.pendingRequests.get(message.id);
        sessionData.pendingRequests.delete(message.id);

        if (!request.res.headersSent) {
          request.res.status(408).json({
            jsonrpc: "2.0",
            id: message.id || null,
            error: {
              code: -32001,
              message: `Request timed out after ${timeoutDuration}ms`,
            },
          });
        }
      }
    }, timeoutDuration);

    if (message.id !== undefined) {
      sessionData.pendingRequests.set(message.id, {
        res,
        timeout: responseTimeout,
        method: message.method,
        timestamp: Date.now(),
        progressInterval,
      });
    }

    const handleResponse = (data) => {
      sessionData.responseBuffer += data.toString();

      let lines = sessionData.responseBuffer.split("\n");
      sessionData.responseBuffer = lines.pop() || "";

      for (const line of lines) {
        const trimmedLine = line.trim();
        if (trimmedLine) {
          try {
            const parsed = JSON.parse(trimmedLine);

            if (parsed.id !== undefined) {
              const pendingRequest = sessionData.pendingRequests.get(parsed.id);
              if (pendingRequest) {
                if (pendingRequest.progressInterval) {
                  clearInterval(pendingRequest.progressInterval);
                }

                clearTimeout(pendingRequest.timeout);
                sessionData.pendingRequests.delete(parsed.id);

                if (pendingRequest.method === "initialize" && !parsed.error) {
                  sessionData.initialized = true;
                  sessionData.initializing = false;

                  if (!parsed.result) parsed.result = {};
                  parsed.result.sessionId = sessionId;

                  if (!parsed.result.protocolVersion) {
                    parsed.result.protocolVersion = "2024-11-05";
                  }
                }

                if (!pendingRequest.res.headersSent) {
                  pendingRequest.res.json(parsed);
                }
                return;
              }
            } else if (parsed.method) {
              // Server notification
              if (sessionData.sseRes && !sessionData.sseRes.writableEnded) {
                sessionData.sseRes.write(`data: ${JSON.stringify(parsed)}\n\n`);
              }
            }
          } catch (e) {
            console.log(`Non-JSON output: ${trimmedLine}`);
          }
        }
      }
    };

    if (!sessionData.listenersSetup) {
      mcpProcess.stdout.on("data", handleResponse);

      mcpProcess.stderr.on("data", (data) => {
        console.error(`MCP Error:`, data.toString().trim());
      });

      mcpProcess.on("exit", (code, signal) => {
        console.error(`MCP process exited (code: ${code}, signal: ${signal})`);
        cleanupSession(sessionId, "process exited");
      });

      sessionData.listenersSetup = true;
    }

    const messageStr = JSON.stringify(message) + "\n";
    console.log("Sending to MCP process:", messageStr.trim());

    if (!mcpProcess.stdin.writable) {
      if (progressInterval) clearInterval(progressInterval);
      throw new Error("MCP process stdin is not writable");
    }

    mcpProcess.stdin.write(messageStr);
  } catch (error) {
    console.error("Error processing MCP request:", error);

    if (
      message.id !== undefined &&
      sessionData.pendingRequests.has(message.id)
    ) {
      const request = sessionData.pendingRequests.get(message.id);
      if (request.progressInterval) clearInterval(request.progressInterval);
      clearTimeout(request.timeout);
      sessionData.pendingRequests.delete(message.id);
    }

    if (!res.headersSent) {
      res.status(500).json({
        jsonrpc: "2.0",
        id: message.id || null,
        error: { code: -32603, message: "Internal error", data: error.message },
      });
    }
  }
});

// Debug endpoints
app.get("/debug/sessions", (req, res) => {
  const sessions = {};
  const now = Date.now();

  activeSessions.forEach((sessionData, sessionId) => {
    sessions[sessionId] = {
      initialized: sessionData.initialized,
      processAlive: sessionData.process && !sessionData.process.killed,
      pendingRequests: sessionData.pendingRequests.size,
      lastActivity: new Date(sessionData.lastActivity).toISOString(),
      age: now - sessionData.createdAt,
    };
  });

  res.json({
    totalSessions: activeSessions.size,
    config: CONFIG,
    sessions,
  });
});

// Start server
app.listen(PORT, () => {
  console.log(`Cloud Compliance MCP Server running on port ${PORT}`);
  console.log(`Health: http://localhost:${PORT}/health`);
  console.log(`MCP endpoint: POST http://localhost:${PORT}/mcp`);
  console.log("\nTimeout Configuration:");
  console.log(`- Request Timeout: ${CONFIG.REQUEST_TIMEOUT}ms`);
  console.log(`- Session Idle: ${CONFIG.SESSION_IDLE_TIMEOUT}ms`);
  console.log(`- Session Max: ${CONFIG.SESSION_MAX_LIFETIME}ms`);

  const hasAWS = !!(
    process.env.AWS_ACCESS_KEY_ID && process.env.AWS_SECRET_ACCESS_KEY
  );
  console.log(`\nAWS: ${hasAWS ? "✓ Configured" : "✗ Missing credentials"}`);
});

// Graceful shutdown
process.on("SIGTERM", () => {
  console.log("Shutting down gracefully...");
  activeSessions.forEach((sessionData, sessionId) => {
    cleanupSession(sessionId, "server shutdown");
  });
  process.exit(0);
});

process.on("SIGINT", () => {
  console.log("Shutting down gracefully...");
  activeSessions.forEach((sessionData, sessionId) => {
    cleanupSession(sessionId, "server shutdown");
  });
  process.exit(0);
});
