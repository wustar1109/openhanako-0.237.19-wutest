// lib/net/outbound-proxy.js

import {
  Agent as UndiciAgent,
  ProxyAgent as UndiciProxyAgent,
  Socks5ProxyAgent,
  getGlobalDispatcher,
  setGlobalDispatcher,
} from "undici";
import { ProxyAgent as NodeProxyAgent } from "proxy-agent";
import {
  normalizeNetworkProxyConfig,
  proxyConfigFromEnvironment,
  resolveProxyForUrl,
} from "../../shared/network-proxy.js";

const originalGlobalDispatcher = getGlobalDispatcher();
let currentConfig = normalizeNetworkProxyConfig();
let currentDispatcher = null;
let nodeProxyAgentCache = new Map();

function proxyProtocol(proxyUrl) {
  try {
    return new URL(proxyUrl).protocol;
  } catch {
    return "";
  }
}

function createUndiciProxyDispatcher(proxyUrl) {
  const protocol = proxyProtocol(proxyUrl);
  if (protocol === "http:" || protocol === "https:") {
    return new UndiciProxyAgent(proxyUrl);
  }
  if (protocol === "socks:" || protocol === "socks5:") {
    return new Socks5ProxyAgent(proxyUrl);
  }
  throw new Error(`unsupported proxy protocol: ${protocol || "(unknown)"}`);
}

function collectUnique(values) {
  return [...new Set(values.filter(Boolean))];
}

function buildEffectiveProxyConfig(config, env) {
  const normalized = normalizeNetworkProxyConfig(config);
  if (normalized.mode === "system") return proxyConfigFromEnvironment(env);
  return normalized;
}

function hasUsableProxy(config) {
  return !!(config.httpProxy || config.httpsProxy || config.wsProxy || config.wssProxy);
}

function createGlobalDispatcher(config, env = process.env) {
  const effective = buildEffectiveProxyConfig(config, env);
  if (effective.mode === "direct" || !hasUsableProxy(effective)) return null;

  const direct = new UndiciAgent();
  const proxyUrls = collectUnique([effective.httpProxy, effective.httpsProxy]);
  const proxyDispatchers = new Map(proxyUrls.map(url => [url, createUndiciProxyDispatcher(url)]));

  return {
    dispatch(opts, handler) {
      const origin = opts?.origin ? String(opts.origin) : "";
      const proxyUrl = origin ? resolveProxyForUrl(origin, effective, env) : "";
      const dispatcher = proxyUrl ? proxyDispatchers.get(proxyUrl) : null;
      return (dispatcher || direct).dispatch(opts, handler);
    },
    async close() {
      await Promise.allSettled([
        direct.close?.(),
        ...[...proxyDispatchers.values()].map(dispatcher => dispatcher.close?.()),
      ]);
    },
    destroy(err) {
      direct.destroy?.(err);
      for (const dispatcher of proxyDispatchers.values()) {
        dispatcher.destroy?.(err);
      }
    },
  };
}

function resetNodeProxyAgentCache() {
  for (const agent of nodeProxyAgentCache.values()) {
    agent.destroy?.();
  }
  nodeProxyAgentCache = new Map();
}

function closeDispatcher(dispatcher) {
  if (!dispatcher) return;
  try {
    dispatcher.close?.().catch?.(() => {});
  } catch {}
}

function describeProxyMode(config, env = process.env) {
  const effective = buildEffectiveProxyConfig(config, env);
  if (config.mode === "system") {
    return hasUsableProxy(effective) ? "system-env" : "system";
  }
  return config.mode;
}

export function createOutboundProxyRuntime({ log = () => {} } = {}) {
  return {
    apply(config) {
      const normalized = normalizeNetworkProxyConfig(config, { strict: true });
      const nextDispatcher = createGlobalDispatcher(normalized);
      closeDispatcher(currentDispatcher);
      resetNodeProxyAgentCache();
      currentConfig = normalized;
      currentDispatcher = nextDispatcher;
      setGlobalDispatcher(nextDispatcher || originalGlobalDispatcher);
      log(`[proxy] outbound mode=${describeProxyMode(normalized)}`);
      return normalized;
    },
    getConfig() {
      return currentConfig;
    },
    reset() {
      closeDispatcher(currentDispatcher);
      resetNodeProxyAgentCache();
      currentConfig = normalizeNetworkProxyConfig();
      currentDispatcher = null;
      setGlobalDispatcher(originalGlobalDispatcher);
    },
  };
}

export function getOutboundProxyConfig() {
  return currentConfig;
}

export function getNodeProxyAgentForUrl(targetUrl, env = process.env) {
  const proxyUrl = resolveProxyForUrl(targetUrl, currentConfig, env);
  if (!proxyUrl) return null;
  let agent = nodeProxyAgentCache.get(proxyUrl);
  if (!agent) {
    agent = new NodeProxyAgent(proxyUrl);
    nodeProxyAgentCache.set(proxyUrl, agent);
  }
  return agent;
}

export function webSocketOptionsForUrl(targetUrl) {
  const agent = getNodeProxyAgentForUrl(targetUrl);
  return agent ? { agent } : {};
}

export function telegramBotOptions(baseOptions = {}) {
  const agent = getNodeProxyAgentForUrl("https://api.telegram.org");
  if (!agent) return { ...baseOptions };
  return {
    ...baseOptions,
    request: {
      ...(baseOptions.request || {}),
      agent,
    },
  };
}

