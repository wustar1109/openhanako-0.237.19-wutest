import { memo, useCallback, useMemo } from 'react';
import type { ChatListItem, ChatMessage } from '../../stores/chat-types';
import { UserMessage } from './UserMessage';
import { AssistantMessage } from './AssistantMessage';

interface Props {
  items: ChatListItem[];
  sessionPath: string;
  agentId?: string | null;
  readOnly?: boolean;
  hideUserIdentity?: boolean;
  userIdentity?: { name?: string | null; avatarUrl?: string | null };
  registerMessageElement?: (messageId: string, element: HTMLDivElement | null) => void;
}

export const ChatTranscript = memo(function ChatTranscript({
  items,
  sessionPath,
  agentId,
  readOnly = false,
  hideUserIdentity = false,
  userIdentity,
  registerMessageElement,
}: Props) {
  const latestTurn = useMemo(() => {
    let latestUserIndex = -1;
    let latestAssistantIndex = -1;
    for (let i = items.length - 1; i >= 0; i -= 1) {
      const item = items[i];
      if (item.type !== 'message') continue;
      if (latestUserIndex < 0 && item.data.role === 'user') latestUserIndex = i;
      if (latestAssistantIndex < 0 && item.data.role === 'assistant') latestAssistantIndex = i;
      if (latestUserIndex >= 0 && latestAssistantIndex >= 0) break;
    }
    const latestUserItem = latestUserIndex >= 0 ? items[latestUserIndex] : null;
    return {
      latestUserIndex,
      latestAssistantIndex,
      latestUserMessage: latestUserItem?.type === 'message' && latestUserItem.data.role === 'user'
        ? latestUserItem.data
        : null,
    };
  }, [items]);

  return (
    <>
      {items.map((item, index) => (
        <TranscriptItemView
          key={item.type === 'message' ? item.data.id : `c-${index}`}
          item={item}
          prevItem={index > 0 ? items[index - 1] : undefined}
          sessionPath={sessionPath}
          agentId={agentId}
          readOnly={readOnly}
          hideUserIdentity={hideUserIdentity}
          userIdentity={userIdentity}
          latestUserMessage={latestTurn.latestUserMessage}
          isLatestUserMessage={index === latestTurn.latestUserIndex}
          isLatestAssistantMessage={
            index === latestTurn.latestAssistantIndex
            && latestTurn.latestAssistantIndex > latestTurn.latestUserIndex
          }
          registerMessageElement={registerMessageElement}
        />
      ))}
    </>
  );
});

const TranscriptItemView = memo(function TranscriptItemView({
  item,
  prevItem,
  sessionPath,
  agentId,
  readOnly,
  hideUserIdentity,
  userIdentity,
  latestUserMessage,
  isLatestUserMessage,
  isLatestAssistantMessage,
  registerMessageElement,
}: {
  item: ChatListItem;
  prevItem?: ChatListItem;
  sessionPath: string;
  agentId?: string | null;
  readOnly: boolean;
  hideUserIdentity: boolean;
  userIdentity?: { name?: string | null; avatarUrl?: string | null };
  latestUserMessage?: ChatMessage | null;
  isLatestUserMessage: boolean;
  isLatestAssistantMessage: boolean;
  registerMessageElement?: (messageId: string, element: HTMLDivElement | null) => void;
}) {
  const messageId = item.type === 'message' ? item.data.id : null;
  const messageRef = useCallback((element: HTMLDivElement | null) => {
    if (messageId) registerMessageElement?.(messageId, element);
  }, [messageId, registerMessageElement]);

  if (item.type === 'compaction') return null;

  const msg = item.data;
  const prevRole = prevItem?.type === 'message' ? prevItem.data.role : null;
  const showAvatar = msg.role !== prevRole;

  if (msg.role === 'user') {
    return (
      <UserMessage
        message={msg}
        showAvatar={showAvatar}
        sessionPath={sessionPath}
        readOnly={readOnly}
        hideIdentity={hideUserIdentity}
        userIdentity={userIdentity}
        isLatestUserMessage={isLatestUserMessage}
        messageRef={messageRef}
      />
    );
  }

  return (
    <AssistantMessage
      message={msg}
      showAvatar={showAvatar}
      sessionPath={sessionPath}
      agentId={agentId}
      readOnly={readOnly}
      isLatestAssistantMessage={isLatestAssistantMessage}
      retrySourceMessage={latestUserMessage}
      messageRef={messageRef}
    />
  );
});
