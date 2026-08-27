from chat_sessions import ChatSessionStore


def test_session_persists_messages_and_generated_title(tmp_path):
    database_path = tmp_path / "chat_sessions.sqlite3"
    store = ChatSessionStore(database_path)
    session = store.create()

    store.add_messages(
        session["id"],
        [{"role": "user", "content": "Can you help me plan a detailed gym schedule for next week with recovery days?"}],
    )

    restored = ChatSessionStore(database_path).get(session["id"])

    assert restored["title"] == "Help me plan a detailed gym schedule for next..."
    assert restored["messages"] == [
        {"role": "user", "content": "Can you help me plan a detailed gym schedule for next week with recovery days?"}
    ]


def test_custom_title_is_not_replaced_by_later_messages(tmp_path):
    store = ChatSessionStore(tmp_path / "chat_sessions.sqlite3")
    session = store.create()
    store.update(session["id"], title="Weekly gym plan")

    updated = store.add_messages(session["id"], [{"role": "user", "content": "Build a workout plan."}])

    assert updated["title"] == "Weekly gym plan"