import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CredentialType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CREDENTIAL_TYPE_UNSPECIFIED: _ClassVar[CredentialType]
    CREDENTIAL_TYPE_OAUTH: _ClassVar[CredentialType]
    CREDENTIAL_TYPE_API_KEY: _ClassVar[CredentialType]

class AllocationStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ALLOCATION_STATUS_UNSPECIFIED: _ClassVar[AllocationStatus]
    ALLOCATION_STATUS_DELIVERING: _ClassVar[AllocationStatus]
    ALLOCATION_STATUS_ACTIVE: _ClassVar[AllocationStatus]
    ALLOCATION_STATUS_INACTIVE: _ClassVar[AllocationStatus]
    ALLOCATION_STATUS_QUARANTINED: _ClassVar[AllocationStatus]
    ALLOCATION_STATUS_RECALLING: _ClassVar[AllocationStatus]
    ALLOCATION_STATUS_ABSENT: _ClassVar[AllocationStatus]

class SwitchMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SWITCH_MODE_UNSPECIFIED: _ClassVar[SwitchMode]
    SWITCH_MODE_AUTO: _ClassVar[SwitchMode]
    SWITCH_MODE_MANUAL: _ClassVar[SwitchMode]

class EncryptionAlgorithm(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ENCRYPTION_ALGORITHM_UNSPECIFIED: _ClassVar[EncryptionAlgorithm]
    ENCRYPTION_ALGORITHM_AES_256_GCM: _ClassVar[EncryptionAlgorithm]
CREDENTIAL_TYPE_UNSPECIFIED: CredentialType
CREDENTIAL_TYPE_OAUTH: CredentialType
CREDENTIAL_TYPE_API_KEY: CredentialType
ALLOCATION_STATUS_UNSPECIFIED: AllocationStatus
ALLOCATION_STATUS_DELIVERING: AllocationStatus
ALLOCATION_STATUS_ACTIVE: AllocationStatus
ALLOCATION_STATUS_INACTIVE: AllocationStatus
ALLOCATION_STATUS_QUARANTINED: AllocationStatus
ALLOCATION_STATUS_RECALLING: AllocationStatus
ALLOCATION_STATUS_ABSENT: AllocationStatus
SWITCH_MODE_UNSPECIFIED: SwitchMode
SWITCH_MODE_AUTO: SwitchMode
SWITCH_MODE_MANUAL: SwitchMode
ENCRYPTION_ALGORITHM_UNSPECIFIED: EncryptionAlgorithm
ENCRYPTION_ALGORITHM_AES_256_GCM: EncryptionAlgorithm

class AccountRef(_message.Message):
    __slots__ = ("ams_account_id", "email", "account_uuid")
    AMS_ACCOUNT_ID_FIELD_NUMBER: _ClassVar[int]
    EMAIL_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_UUID_FIELD_NUMBER: _ClassVar[int]
    ams_account_id: str
    email: str
    account_uuid: str
    def __init__(self, ams_account_id: _Optional[str] = ..., email: _Optional[str] = ..., account_uuid: _Optional[str] = ...) -> None: ...

class EncryptedCredential(_message.Message):
    __slots__ = ("algorithm", "ciphertext", "nonce", "key_id", "aad_ams_account_id", "aad_agent_id")
    ALGORITHM_FIELD_NUMBER: _ClassVar[int]
    CIPHERTEXT_FIELD_NUMBER: _ClassVar[int]
    NONCE_FIELD_NUMBER: _ClassVar[int]
    KEY_ID_FIELD_NUMBER: _ClassVar[int]
    AAD_AMS_ACCOUNT_ID_FIELD_NUMBER: _ClassVar[int]
    AAD_AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    algorithm: EncryptionAlgorithm
    ciphertext: bytes
    nonce: bytes
    key_id: str
    aad_ams_account_id: str
    aad_agent_id: str
    def __init__(self, algorithm: _Optional[_Union[EncryptionAlgorithm, str]] = ..., ciphertext: _Optional[bytes] = ..., nonce: _Optional[bytes] = ..., key_id: _Optional[str] = ..., aad_ams_account_id: _Optional[str] = ..., aad_agent_id: _Optional[str] = ...) -> None: ...

class UsageWindow(_message.Message):
    __slots__ = ("pct", "resets_at")
    PCT_FIELD_NUMBER: _ClassVar[int]
    RESETS_AT_FIELD_NUMBER: _ClassVar[int]
    pct: float
    resets_at: _timestamp_pb2.Timestamp
    def __init__(self, pct: _Optional[float] = ..., resets_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class AccountUsage(_message.Message):
    __slots__ = ("account", "allocation_status", "is_current", "five_hour", "seven_day", "usage_fetched_at")
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    ALLOCATION_STATUS_FIELD_NUMBER: _ClassVar[int]
    IS_CURRENT_FIELD_NUMBER: _ClassVar[int]
    FIVE_HOUR_FIELD_NUMBER: _ClassVar[int]
    SEVEN_DAY_FIELD_NUMBER: _ClassVar[int]
    USAGE_FETCHED_AT_FIELD_NUMBER: _ClassVar[int]
    account: AccountRef
    allocation_status: AllocationStatus
    is_current: bool
    five_hour: UsageWindow
    seven_day: UsageWindow
    usage_fetched_at: _timestamp_pb2.Timestamp
    def __init__(self, account: _Optional[_Union[AccountRef, _Mapping]] = ..., allocation_status: _Optional[_Union[AllocationStatus, str]] = ..., is_current: _Optional[bool] = ..., five_hour: _Optional[_Union[UsageWindow, _Mapping]] = ..., seven_day: _Optional[_Union[UsageWindow, _Mapping]] = ..., usage_fetched_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class PoolSummary(_message.Message):
    __slots__ = ("total", "active", "eligible", "quarantined", "all_exhausted", "max_utilization_pct")
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_FIELD_NUMBER: _ClassVar[int]
    ELIGIBLE_FIELD_NUMBER: _ClassVar[int]
    QUARANTINED_FIELD_NUMBER: _ClassVar[int]
    ALL_EXHAUSTED_FIELD_NUMBER: _ClassVar[int]
    MAX_UTILIZATION_PCT_FIELD_NUMBER: _ClassVar[int]
    total: int
    active: int
    eligible: int
    quarantined: int
    all_exhausted: bool
    max_utilization_pct: float
    def __init__(self, total: _Optional[int] = ..., active: _Optional[int] = ..., eligible: _Optional[int] = ..., quarantined: _Optional[int] = ..., all_exhausted: _Optional[bool] = ..., max_utilization_pct: _Optional[float] = ...) -> None: ...

class Ack(_message.Message):
    __slots__ = ("accepted", "message", "received_at")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RECEIVED_AT_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    message: str
    received_at: _timestamp_pb2.Timestamp
    def __init__(self, accepted: _Optional[bool] = ..., message: _Optional[str] = ..., received_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class AmsCommand(_message.Message):
    __slots__ = ("command_id", "signature", "issued_at", "deliver", "recall", "set_active", "set_mode", "switch_now", "req_report", "session_setup")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    ISSUED_AT_FIELD_NUMBER: _ClassVar[int]
    DELIVER_FIELD_NUMBER: _ClassVar[int]
    RECALL_FIELD_NUMBER: _ClassVar[int]
    SET_ACTIVE_FIELD_NUMBER: _ClassVar[int]
    SET_MODE_FIELD_NUMBER: _ClassVar[int]
    SWITCH_NOW_FIELD_NUMBER: _ClassVar[int]
    REQ_REPORT_FIELD_NUMBER: _ClassVar[int]
    SESSION_SETUP_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    signature: bytes
    issued_at: _timestamp_pb2.Timestamp
    deliver: DeliverAccount
    recall: RecallAccount
    set_active: SetAccountActive
    set_mode: SetSwitchMode
    switch_now: SwitchNow
    req_report: RequestReport
    session_setup: SessionSetup
    def __init__(self, command_id: _Optional[str] = ..., signature: _Optional[bytes] = ..., issued_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., deliver: _Optional[_Union[DeliverAccount, _Mapping]] = ..., recall: _Optional[_Union[RecallAccount, _Mapping]] = ..., set_active: _Optional[_Union[SetAccountActive, _Mapping]] = ..., set_mode: _Optional[_Union[SetSwitchMode, _Mapping]] = ..., switch_now: _Optional[_Union[SwitchNow, _Mapping]] = ..., req_report: _Optional[_Union[RequestReport, _Mapping]] = ..., session_setup: _Optional[_Union[SessionSetup, _Mapping]] = ...) -> None: ...

class SessionSetup(_message.Message):
    __slots__ = ("server_credential", "keys", "active_key_id", "revoked_key_ids")
    class WrappedKey(_message.Message):
        __slots__ = ("key_id", "wrapped_key", "algorithm", "not_after")
        KEY_ID_FIELD_NUMBER: _ClassVar[int]
        WRAPPED_KEY_FIELD_NUMBER: _ClassVar[int]
        ALGORITHM_FIELD_NUMBER: _ClassVar[int]
        NOT_AFTER_FIELD_NUMBER: _ClassVar[int]
        key_id: str
        wrapped_key: bytes
        algorithm: EncryptionAlgorithm
        not_after: _timestamp_pb2.Timestamp
        def __init__(self, key_id: _Optional[str] = ..., wrapped_key: _Optional[bytes] = ..., algorithm: _Optional[_Union[EncryptionAlgorithm, str]] = ..., not_after: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
    SERVER_CREDENTIAL_FIELD_NUMBER: _ClassVar[int]
    KEYS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_KEY_ID_FIELD_NUMBER: _ClassVar[int]
    REVOKED_KEY_IDS_FIELD_NUMBER: _ClassVar[int]
    server_credential: str
    keys: _containers.RepeatedCompositeFieldContainer[SessionSetup.WrappedKey]
    active_key_id: str
    revoked_key_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, server_credential: _Optional[str] = ..., keys: _Optional[_Iterable[_Union[SessionSetup.WrappedKey, _Mapping]]] = ..., active_key_id: _Optional[str] = ..., revoked_key_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class DeliverAccount(_message.Message):
    __slots__ = ("assignment_id", "account", "credential_type", "encrypted_credential", "desired_status", "organization_name", "credential_expires_at")
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_TYPE_FIELD_NUMBER: _ClassVar[int]
    ENCRYPTED_CREDENTIAL_FIELD_NUMBER: _ClassVar[int]
    DESIRED_STATUS_FIELD_NUMBER: _ClassVar[int]
    ORGANIZATION_NAME_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    assignment_id: str
    account: AccountRef
    credential_type: CredentialType
    encrypted_credential: EncryptedCredential
    desired_status: AllocationStatus
    organization_name: str
    credential_expires_at: _timestamp_pb2.Timestamp
    def __init__(self, assignment_id: _Optional[str] = ..., account: _Optional[_Union[AccountRef, _Mapping]] = ..., credential_type: _Optional[_Union[CredentialType, str]] = ..., encrypted_credential: _Optional[_Union[EncryptedCredential, _Mapping]] = ..., desired_status: _Optional[_Union[AllocationStatus, str]] = ..., organization_name: _Optional[str] = ..., credential_expires_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class RecallAccount(_message.Message):
    __slots__ = ("assignment_id", "account", "purge_local_copy")
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    PURGE_LOCAL_COPY_FIELD_NUMBER: _ClassVar[int]
    assignment_id: str
    account: AccountRef
    purge_local_copy: bool
    def __init__(self, assignment_id: _Optional[str] = ..., account: _Optional[_Union[AccountRef, _Mapping]] = ..., purge_local_copy: _Optional[bool] = ...) -> None: ...

class SetAccountActive(_message.Message):
    __slots__ = ("assignment_id", "account", "active", "clear_quarantine")
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_FIELD_NUMBER: _ClassVar[int]
    CLEAR_QUARANTINE_FIELD_NUMBER: _ClassVar[int]
    assignment_id: str
    account: AccountRef
    active: bool
    clear_quarantine: bool
    def __init__(self, assignment_id: _Optional[str] = ..., account: _Optional[_Union[AccountRef, _Mapping]] = ..., active: _Optional[bool] = ..., clear_quarantine: _Optional[bool] = ...) -> None: ...

class SetSwitchMode(_message.Message):
    __slots__ = ("mode",)
    MODE_FIELD_NUMBER: _ClassVar[int]
    mode: SwitchMode
    def __init__(self, mode: _Optional[_Union[SwitchMode, str]] = ...) -> None: ...

class SwitchNow(_message.Message):
    __slots__ = ("account", "strategy", "assignment_id")
    class SwitchStrategy(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        SWITCH_STRATEGY_UNSPECIFIED: _ClassVar[SwitchNow.SwitchStrategy]
        SWITCH_STRATEGY_BEST: _ClassVar[SwitchNow.SwitchStrategy]
        SWITCH_STRATEGY_NEXT_AVAILABLE: _ClassVar[SwitchNow.SwitchStrategy]
    SWITCH_STRATEGY_UNSPECIFIED: SwitchNow.SwitchStrategy
    SWITCH_STRATEGY_BEST: SwitchNow.SwitchStrategy
    SWITCH_STRATEGY_NEXT_AVAILABLE: SwitchNow.SwitchStrategy
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    STRATEGY_FIELD_NUMBER: _ClassVar[int]
    ASSIGNMENT_ID_FIELD_NUMBER: _ClassVar[int]
    account: AccountRef
    strategy: SwitchNow.SwitchStrategy
    assignment_id: str
    def __init__(self, account: _Optional[_Union[AccountRef, _Mapping]] = ..., strategy: _Optional[_Union[SwitchNow.SwitchStrategy, str]] = ..., assignment_id: _Optional[str] = ...) -> None: ...

class RequestReport(_message.Message):
    __slots__ = ("report_type", "reason")
    class ReportType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        REPORT_TYPE_UNSPECIFIED: _ClassVar[RequestReport.ReportType]
        REPORT_TYPE_USAGE: _ClassVar[RequestReport.ReportType]
    REPORT_TYPE_UNSPECIFIED: RequestReport.ReportType
    REPORT_TYPE_USAGE: RequestReport.ReportType
    REPORT_TYPE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    report_type: RequestReport.ReportType
    reason: str
    def __init__(self, report_type: _Optional[_Union[RequestReport.ReportType, str]] = ..., reason: _Optional[str] = ...) -> None: ...

class AmaMessage(_message.Message):
    __slots__ = ("register", "hb", "usage", "ack", "event")
    REGISTER_FIELD_NUMBER: _ClassVar[int]
    HB_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    ACK_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    register: Register
    hb: Heartbeat
    usage: UsageReport
    ack: CommandAck
    event: AccountEvent
    def __init__(self, register: _Optional[_Union[Register, _Mapping]] = ..., hb: _Optional[_Union[Heartbeat, _Mapping]] = ..., usage: _Optional[_Union[UsageReport, _Mapping]] = ..., ack: _Optional[_Union[CommandAck, _Mapping]] = ..., event: _Optional[_Union[AccountEvent, _Mapping]] = ...) -> None: ...

class Register(_message.Message):
    __slots__ = ("agent_id", "server_id", "enroll_token", "server_credential", "hostname", "agent_version", "tsamx_version", "switch_mode", "accounts", "applied_command_ids")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    SERVER_ID_FIELD_NUMBER: _ClassVar[int]
    ENROLL_TOKEN_FIELD_NUMBER: _ClassVar[int]
    SERVER_CREDENTIAL_FIELD_NUMBER: _ClassVar[int]
    HOSTNAME_FIELD_NUMBER: _ClassVar[int]
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    TSAMX_VERSION_FIELD_NUMBER: _ClassVar[int]
    SWITCH_MODE_FIELD_NUMBER: _ClassVar[int]
    ACCOUNTS_FIELD_NUMBER: _ClassVar[int]
    APPLIED_COMMAND_IDS_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    server_id: str
    enroll_token: str
    server_credential: str
    hostname: str
    agent_version: str
    tsamx_version: str
    switch_mode: SwitchMode
    accounts: _containers.RepeatedCompositeFieldContainer[AccountUsage]
    applied_command_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, agent_id: _Optional[str] = ..., server_id: _Optional[str] = ..., enroll_token: _Optional[str] = ..., server_credential: _Optional[str] = ..., hostname: _Optional[str] = ..., agent_version: _Optional[str] = ..., tsamx_version: _Optional[str] = ..., switch_mode: _Optional[_Union[SwitchMode, str]] = ..., accounts: _Optional[_Iterable[_Union[AccountUsage, _Mapping]]] = ..., applied_command_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class Heartbeat(_message.Message):
    __slots__ = ("agent_id", "sent_at", "active_account", "switch_mode", "tsamx_healthy", "outbox_depth")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    SENT_AT_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    SWITCH_MODE_FIELD_NUMBER: _ClassVar[int]
    TSAMX_HEALTHY_FIELD_NUMBER: _ClassVar[int]
    OUTBOX_DEPTH_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    sent_at: _timestamp_pb2.Timestamp
    active_account: AccountRef
    switch_mode: SwitchMode
    tsamx_healthy: bool
    outbox_depth: int
    def __init__(self, agent_id: _Optional[str] = ..., sent_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., active_account: _Optional[_Union[AccountRef, _Mapping]] = ..., switch_mode: _Optional[_Union[SwitchMode, str]] = ..., tsamx_healthy: _Optional[bool] = ..., outbox_depth: _Optional[int] = ...) -> None: ...

class UsageReport(_message.Message):
    __slots__ = ("schema_version", "agent_id", "generated_at", "trigger", "active_account", "pool_summary", "accounts", "in_response_to_command_id")
    class Trigger(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        TRIGGER_UNSPECIFIED: _ClassVar[UsageReport.Trigger]
        TRIGGER_SCHEDULE: _ClassVar[UsageReport.Trigger]
        TRIGGER_AMS_QUERY: _ClassVar[UsageReport.Trigger]
        TRIGGER_SWITCH: _ClassVar[UsageReport.Trigger]
    TRIGGER_UNSPECIFIED: UsageReport.Trigger
    TRIGGER_SCHEDULE: UsageReport.Trigger
    TRIGGER_AMS_QUERY: UsageReport.Trigger
    TRIGGER_SWITCH: UsageReport.Trigger
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATED_AT_FIELD_NUMBER: _ClassVar[int]
    TRIGGER_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    POOL_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    ACCOUNTS_FIELD_NUMBER: _ClassVar[int]
    IN_RESPONSE_TO_COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    schema_version: int
    agent_id: str
    generated_at: _timestamp_pb2.Timestamp
    trigger: UsageReport.Trigger
    active_account: AccountRef
    pool_summary: PoolSummary
    accounts: _containers.RepeatedCompositeFieldContainer[AccountUsage]
    in_response_to_command_id: str
    def __init__(self, schema_version: _Optional[int] = ..., agent_id: _Optional[str] = ..., generated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., trigger: _Optional[_Union[UsageReport.Trigger, str]] = ..., active_account: _Optional[_Union[AccountRef, _Mapping]] = ..., pool_summary: _Optional[_Union[PoolSummary, _Mapping]] = ..., accounts: _Optional[_Iterable[_Union[AccountUsage, _Mapping]]] = ..., in_response_to_command_id: _Optional[str] = ...) -> None: ...

class ReportEnvelope(_message.Message):
    __slots__ = ("server_credential", "report")
    SERVER_CREDENTIAL_FIELD_NUMBER: _ClassVar[int]
    REPORT_FIELD_NUMBER: _ClassVar[int]
    server_credential: str
    report: UsageReport
    def __init__(self, server_credential: _Optional[str] = ..., report: _Optional[_Union[UsageReport, _Mapping]] = ...) -> None: ...

class CommandAck(_message.Message):
    __slots__ = ("command_id", "agent_id", "observed_at", "convergence", "account_state", "switch_mode", "pool_summary", "detail", "error_code")
    class Convergence(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        CONVERGENCE_UNSPECIFIED: _ClassVar[CommandAck.Convergence]
        CONVERGENCE_CONVERGED: _ClassVar[CommandAck.Convergence]
        CONVERGENCE_PENDING: _ClassVar[CommandAck.Convergence]
        CONVERGENCE_DIVERGED: _ClassVar[CommandAck.Convergence]
        CONVERGENCE_REJECTED: _ClassVar[CommandAck.Convergence]
    CONVERGENCE_UNSPECIFIED: CommandAck.Convergence
    CONVERGENCE_CONVERGED: CommandAck.Convergence
    CONVERGENCE_PENDING: CommandAck.Convergence
    CONVERGENCE_DIVERGED: CommandAck.Convergence
    CONVERGENCE_REJECTED: CommandAck.Convergence
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_AT_FIELD_NUMBER: _ClassVar[int]
    CONVERGENCE_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_STATE_FIELD_NUMBER: _ClassVar[int]
    SWITCH_MODE_FIELD_NUMBER: _ClassVar[int]
    POOL_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    agent_id: str
    observed_at: _timestamp_pb2.Timestamp
    convergence: CommandAck.Convergence
    account_state: AccountUsage
    switch_mode: SwitchMode
    pool_summary: PoolSummary
    detail: str
    error_code: str
    def __init__(self, command_id: _Optional[str] = ..., agent_id: _Optional[str] = ..., observed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., convergence: _Optional[_Union[CommandAck.Convergence, str]] = ..., account_state: _Optional[_Union[AccountUsage, _Mapping]] = ..., switch_mode: _Optional[_Union[SwitchMode, str]] = ..., pool_summary: _Optional[_Union[PoolSummary, _Mapping]] = ..., detail: _Optional[str] = ..., error_code: _Optional[str] = ...) -> None: ...

class AccountEvent(_message.Message):
    __slots__ = ("schema_version", "agent_id", "event_id", "occurred_at", "kind", "trigger", "to", "pool_summary", "detail")
    class Kind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        KIND_UNSPECIFIED: _ClassVar[AccountEvent.Kind]
        KIND_SWITCH: _ClassVar[AccountEvent.Kind]
        KIND_QUARANTINE: _ClassVar[AccountEvent.Kind]
        KIND_ALL_EXHAUSTED: _ClassVar[AccountEvent.Kind]
    KIND_UNSPECIFIED: AccountEvent.Kind
    KIND_SWITCH: AccountEvent.Kind
    KIND_QUARANTINE: AccountEvent.Kind
    KIND_ALL_EXHAUSTED: AccountEvent.Kind
    class Trigger(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        TRIGGER_UNSPECIFIED: _ClassVar[AccountEvent.Trigger]
        TRIGGER_AT_LIMIT: _ClassVar[AccountEvent.Trigger]
        TRIGGER_MANUAL: _ClassVar[AccountEvent.Trigger]
        TRIGGER_FAILOVER: _ClassVar[AccountEvent.Trigger]
    TRIGGER_UNSPECIFIED: AccountEvent.Trigger
    TRIGGER_AT_LIMIT: AccountEvent.Trigger
    TRIGGER_MANUAL: AccountEvent.Trigger
    TRIGGER_FAILOVER: AccountEvent.Trigger
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    OCCURRED_AT_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    TRIGGER_FIELD_NUMBER: _ClassVar[int]
    FROM_FIELD_NUMBER: _ClassVar[int]
    TO_FIELD_NUMBER: _ClassVar[int]
    POOL_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    schema_version: int
    agent_id: str
    event_id: str
    occurred_at: _timestamp_pb2.Timestamp
    kind: AccountEvent.Kind
    trigger: AccountEvent.Trigger
    to: AccountRef
    pool_summary: PoolSummary
    detail: str
    def __init__(self, schema_version: _Optional[int] = ..., agent_id: _Optional[str] = ..., event_id: _Optional[str] = ..., occurred_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., kind: _Optional[_Union[AccountEvent.Kind, str]] = ..., trigger: _Optional[_Union[AccountEvent.Trigger, str]] = ..., to: _Optional[_Union[AccountRef, _Mapping]] = ..., pool_summary: _Optional[_Union[PoolSummary, _Mapping]] = ..., detail: _Optional[str] = ..., **kwargs) -> None: ...
