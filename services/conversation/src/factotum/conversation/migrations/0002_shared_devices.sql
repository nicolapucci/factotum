-- What is said on a shared device is kept only as long as a shared device can see it (a minute or so),
-- so it is forgotten often, by its owner kind alone: an index for that, and nothing else.
CREATE INDEX exchanges_shared_time ON conversation.exchanges (created_at) WHERE owner_kind = 'tenant';
CREATE INDEX conversations_shared_last ON conversation.conversations (last_at) WHERE owner_kind = 'tenant';
