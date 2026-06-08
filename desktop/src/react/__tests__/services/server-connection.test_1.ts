import { describe, expect, it, vi } from 'vitest';

import {
  appendConnectionAuth,
  buildConnectionUrl,
  buildConnectionWsUrl,
  connectDeviceServerConnection,
  createDeviceServerConnection,
  createLocalServerConnection,
  hasServerConnection,
  mergeServerIdentity,
  persistServerConnectionSelection,
  readPersistedServerConnectionState,
  refreshLocalServerConnection,
  resolveServerConnection,
  upsertServerConnection,
  writePersistedServerConnectionState,
} from '../../services/server-connection';

describe('server connection helpers', () => {
  it('creates the local default ServerConnection from port and token', () => {
    expect(createLocalServerConnection({
      serverPort: 3210,
      serverToken: 'test-token-123',
    })).toEqual({
      connectionId: 'local',
      kind: 'local',
      serverId: 'local',
      studioId: 'local',
      label: 'Local Hana',
      baseUrl: 'http://127.0.0.1:3210',
      wsUrl: 'ws://127.0.0.1:3210',
      token: 'test-token-123',
      authState: 'paired',
      trustState: 'local',
      credentialKind: 'loopback_token',
      platformAccountId: null,
      officialServiceKind: null,
      capabilities: ['chat', 'resources', 'files', 'tools'],
    });
  });

  it('returns null when local server port is not ready', () => {
    expect(createLocalServerConnection({
      serverPort: null,
      serverToken: 'test-token-123',
    })).toBeNull();
  });

  it('reports readiness from active connection while preserving legacy compatibility', () => {
    const active = createLocalServerConnection({
      serverPort: 4242,
      serverToken: 'active-token',
    });

    expect(hasServerConnection({ activeServerConnection: active })).toBe(true);
    expect(hasServerConnection({ serverPort: 3210, serverToken: 'legacy-token' })).toBe(true);
    expect(hasServerConnection({ serverPort: null, serverToken: 'legacy-token' })).toBe(false);
  });

  it('prefers the active connection over legacy port and token fields', () => {
    const active = createLocalServerConnection({
      serverPort: 4242,
      serverToken: 'active-token',
    });

    expect(resolveServerConnection({
      activeServerConnection: active,
      serverPort: 3210,
      serverToken: 'legacy-token',
    })).toBe(active);
  });

  it('resolves the active connection from the StudioConnection registry before legacy mirror fields', () => {
    const registryConnection = mergeServerIdentity(createLocalServerConnection({
      serverPort: 4242,
      serverToken: 'registry-token',
    })!, {
      serverId: 'server_registry',
      userId: 'user_registry',
      studioId: 'studio_registry',
      label: 'Registry Studio',
    });
    const staleMirror = createLocalServerConnection({
      serverPort: 3210,
      serverToken: 'stale-token',
    });

    expect(resolveServerConnection({
      activeServerConnectionId: 'local',
      serverConnections: { local: registryConnection },
      activeServerConnection: staleMirror,
      serverPort: 3210,
      serverToken: 'legacy-token',
    })).toBe(registryConnection);
  });

  it('upserts connections by connectionId without mutating the previous registry', () => {
    const local = createLocalServerConnection({
      serverPort: 3210,
      serverToken: 'local-token',
    })!;
    const remote = {
      ...local,
      connectionId: 'custom:remote',
      kind: 'custom_remote' as const,
      label: 'Remote Studio',
      baseUrl: 'https://hana.example',
      wsUrl: 'wss://hana.example',
      token: 'remote-token',
      trustState: 'tunnel' as const,
      credentialKind: 'device_credential' as const,
    };
    const previousRegistry = { local };
    const registry = upsertServerConnection(previousRegistry, remote);

    expect(Object.keys(registry)).toEqual(['local', 'custom:remote']);
    expect(registry['custom:remote']).toBe(remote);
    expect(registry).not.toBe(previousRegistry);
    expect(previousRegistry).toEqual({ local });
  });

  it('rejects registry entries that violate the trusted access contract', () => {
    const local = createLocalServerConnection({
      serverPort: 3210,
      serverToken: 'local-token',
    })!;
    const invalidRemote = {
      ...local,
      connectionId: 'lan:bad',
      kind: 'lan' as const,
      label: 'Bad LAN Studio',
      baseUrl: 'http://192.168.1.20:14500',
      wsUrl: 'ws://192.168.1.20:14500',
      trustState: 'lan' as const,
      credentialKind: 'loopback_token' as const,
    };

    expect(() => upsertServerConnection({ local }, invalidRemote))
      .toThrow('lan connection must not use loopback_token');
  });

  it('builds browser-loadable URLs with query token while preserving existing query params', () => {
    const connection = createLocalServerConnection({
      serverPort: '3210',
      serverToken: 'test-token-123',
    });
    expect(connection).not.toBeNull();

    expect(buildConnectionUrl(connection!, '/api/agents/hana/avatar', { includeTokenQuery: true }))
      .toBe('http://127.0.0.1:3210/api/agents/hana/avatar?token=test-token-123');
    expect(buildConnectionUrl(connection!, '/api/sessions?limit=10', { includeTokenQuery: true }))
      .toBe('http://127.0.0.1:3210/api/sessions?limit=10&token=test-token-123');
  });

  it('does not put remote device credentials into browser-loadable URL query strings', () => {
    const local = createLocalServerConnection({
      serverPort: '3210',
      serverToken: 'local-token',
    })!;
    const remote = {
      ...local,
      connectionId: 'custom:remote',
      kind: 'custom_remote' as const,
      label: 'Remote Studio',
      baseUrl: 'https://hana.example',
      wsUrl: 'wss://hana.example',
      token: 'remote-token',
      trustState: 'tunnel' as const,
      credentialKind: 'device_credential' as const,
    };

    expect(buildConnectionUrl(remote, '/api/resources/res_1/content', { includeTokenQuery: true }))
      .toBe('https://hana.example/api/resources/res_1/content');
    expect(buildConnectionWsUrl(remote, '/ws')).toBe('wss://hana.example/ws');
  });

  it('creates a LAN device ServerConnection from manual URL, credential, and server identity', () => {
    const connection = createDeviceServerConnection({
      baseUrl: '192.168.31.75:14500/mobile/',
      credential: 'hana_dev_remote_secret',
      identity: {
        connectionKind: 'lan',
        serverId: 'server_lan',
        serverNodeId: 'node_lan',
        userId: 'user_lan',
        studioId: 'studio_lan',
        label: 'LAN Server',
        studioLabel: 'Personal Studio',
        trustState: 'lan',
        authState: 'paired',
        credentialKind: 'device_credential',
        capabilities: ['chat', 'resources', 'files'],
      },
    });

    expect(connection).toMatchObject({
      connectionId: 'lan:node_lan:studio_lan',
      kind: 'lan',
      serverId: 'server_lan',
      serverNodeId: 'node_lan',
      studioId: 'studio_lan',
      label: 'Personal Studio',
      baseUrl: 'http://192.168.31.75:14500',
      wsUrl: 'ws://192.168.31.75:14500',
      token: 'hana_dev_remote_secret',
      trustState: 'lan',
      credentialKind: 'device_credential',
      capabilities: ['chat', 'resources', 'files'],
    });
  });

  it('logs in once before creating a manual LAN connection so WebSocket can use the web session cookie', async () => {
    const fetchImpl = vi.fn(async (url: string) => {
      if (url === 'http://192.168.31.75:14500/api/web-auth/login') {
        return { ok: true, json: async () => ({ ok: true }) } as Response;
      }
      if (url === 'http://192.168.31.75:14500/api/server/identity') {
        return {
          ok: true,
          json: async () => ({
            connectionKind: 'lan',
            serverId: 'server_lan',
            serverNodeId: 'node_lan',
            userId: 'user_lan',
            studioId: 'studio_lan',
            label: 'LAN Server',
            trustState: 'lan',
            authState: 'paired',
            credentialKind: 'device_credential',
            capabilities: ['chat', 'resources', 'files'],
          }),
        } as Response;
      }
      throw new Error(`unexpected URL ${url}`);
    });

    const connection = await connectDeviceServerConnection({
      baseUrl: 'http://192.168.31.75:14500/',
      credential: 'hana_dev_remote_secret',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    expect(fetchImpl).toHaveBeenNthCalledWith(1, 'http://192.168.31.75:14500/api/web-auth/login', expect.objectContaining({
      method: 'POST',
      credentials: 'include',
      body: JSON.stringify({ credential: 'hana_dev_remote_secret' }),
    }));
    expect(fetchImpl).toHaveBeenNthCalledWith(2, 'http://192.168.31.75:14500/api/server/identity', expect.objectContaining({
      headers: { Authorization: 'Bearer hana_dev_remote_secret' },
      credentials: 'include',
    }));
    expect(connection.connectionId).toBe('lan:node_lan:studio_lan');
  });

  it('persists only non-local ServerConnections and the active remote selection', () => {
    const storageData = new Map<string, string>();
    const storage = {
      getItem: (key: string) => storageData.get(key) ?? null,
      setItem: (key: string, value: string) => { storageData.set(key, value); },
      removeItem: (key: string) => { storageData.delete(key); },
    };
    const local = createLocalServerConnection({
      serverPort: 3210,
      serverToken: 'local-token',
    })!;
    const remote = createDeviceServerConnection({
      baseUrl: 'http://192.168.31.75:14500',
      credential: 'hana_dev_remote_secret',
      identity: {
        connectionKind: 'lan',
        serverId: 'server_lan',
        serverNodeId: 'node_lan',
        userId: 'user_lan',
        studioId: 'studio_lan',
        label: 'LAN Server',
        trustState: 'lan',
        authState: 'paired',
        credentialKind: 'device_credential',
        capabilities: ['chat'],
      },
    });

    writePersistedServerConnectionState({
      serverConnections: { local, [remote.connectionId]: remote },
      activeServerConnectionId: remote.connectionId,
    }, storage);

    const loaded = readPersistedServerConnectionState(storage);
    expect(Object.keys(loaded.serverConnections)).toEqual([remote.connectionId]);
    expect(loaded.activeServerConnectionId).toBe(remote.connectionId);

    const selected = persistServerConnectionSelection(remote, storage);
    expect(selected.activeServerConnectionId).toBe(remote.connectionId);
  });

  it('builds fetch URLs without leaking token into the query string', () => {
    const connection = createLocalServerConnection({
      serverPort: '3210',
      serverToken: 'test-token-123',
    });
    expect(connection).not.toBeNull();

    expect(buildConnectionUrl(connection!, '/api/health')).toBe('http://127.0.0.1:3210/api/health');
  });

  it('injects Authorization while preserving caller headers', () => {
    const connection = createLocalServerConnection({
      serverPort: '3210',
      serverToken: 'test-token-123',
    });
    expect(connection).not.toBeNull();

    expect(appendConnectionAuth(connection!, { 'Content-Type': 'application/json' })).toEqual({
      'Content-Type': 'application/json',
      Authorization: 'Bearer test-token-123',
    });
  });

  it('builds WebSocket URLs with query token for browser WebSocket auth', () => {
    const connection = createLocalServerConnection({
      serverPort: '3210',
      serverToken: 'test-token-123',
    });
    expect(connection).not.toBeNull();

    expect(buildConnectionWsUrl(connection!, '/ws')).toBe('ws://127.0.0.1:3210/ws?token=test-token-123');
  });

  it('merges stable server identity without changing transport details', () => {
    const connection = createLocalServerConnection({
      serverPort: '3210',
      serverToken: 'test-token-123',
    });
    expect(connection).not.toBeNull();

    expect(mergeServerIdentity(connection!, {
      connectionKind: 'local',
      serverId: 'server_stable',
      userId: 'user_stable',
      studioId: 'studio_stable',
      label: 'Stable Server',
      userLabel: 'Stable User',
      studioLabel: 'Stable Studio',
      authState: 'paired',
      trustState: 'local',
      capabilities: ['chat', 'resources', 'tools', 'identity'],
      version: '1.2.3',
    })).toEqual({
      connectionId: 'local',
      serverId: 'server_stable',
      userId: 'user_stable',
      studioId: 'studio_stable',
      label: 'Stable Server',
      userLabel: 'Stable User',
      studioLabel: 'Stable Studio',
      serverVersion: '1.2.3',
      kind: 'local',
      baseUrl: 'http://127.0.0.1:3210',
      wsUrl: 'ws://127.0.0.1:3210',
      token: 'test-token-123',
      authState: 'paired',
      trustState: 'local',
      credentialKind: 'loopback_token',
      platformAccountId: null,
      officialServiceKind: null,
      capabilities: ['chat', 'resources', 'tools', 'identity'],
    });
  });

  it('keeps ServerNode execution scope from identity metadata', () => {
    const connection = createLocalServerConnection({
      serverPort: '3210',
      serverToken: 'test-token-123',
    });
    expect(connection).not.toBeNull();

    expect(mergeServerIdentity(connection!, {
      connectionKind: 'local',
      serverId: 'server_stable',
      serverNodeId: 'node_stable',
      serverNodeKind: 'local',
      serverNodeTransport: 'loopback',
      userId: 'user_stable',
      studioId: 'studio_stable',
      label: 'Stable Server',
      executionBoundary: {
        schemaVersion: 1,
        boundaryId: 'execb_node_stable_studio_stable',
        kind: 'local_process',
        serverNodeId: 'node_stable',
        studioId: 'studio_stable',
      },
    })).toMatchObject({
      serverId: 'server_stable',
      serverNodeId: 'node_stable',
      serverNodeKind: 'local',
      serverNodeTransport: 'loopback',
      executionBoundary: {
        boundaryId: 'execb_node_stable_studio_stable',
        serverNodeId: 'node_stable',
        studioId: 'studio_stable',
      },
    });
  });

  it('refreshes local transport without drifting stable server/user/space identity', () => {
    const connection = mergeServerIdentity(createLocalServerConnection({
      serverPort: '3210',
      serverToken: 'old-token',
    })!, {
      serverId: 'server_stable',
      userId: 'user_stable',
      studioId: 'studio_stable',
      label: 'Stable Server',
      userLabel: 'Stable User',
      studioLabel: 'Stable Studio',
      capabilities: ['chat', 'resources', 'tools', 'identity'],
    });

    expect(refreshLocalServerConnection({
      existingConnection: connection,
      serverPort: '4222',
      serverToken: 'new-token',
    })).toEqual({
      ...connection,
      connectionId: 'local',
      baseUrl: 'http://127.0.0.1:4222',
      wsUrl: 'ws://127.0.0.1:4222',
      token: 'new-token',
      authState: 'paired',
      trustState: 'local',
      credentialKind: 'loopback_token',
      platformAccountId: null,
      officialServiceKind: null,
    });
  });
});
