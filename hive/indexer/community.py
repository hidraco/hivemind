"""[WIP] Process community ops."""

#pylint: disable=too-many-lines

import logging
import re
import ujson as json

from hive.db.adapter import Db
from hive.indexer.accounts import Accounts

log = logging.getLogger(__name__)

DB = Db.instance()

ROLES = {'owner': 8, 'admin': 6, 'mod': 4, 'member': 2, 'guest': 0, 'muted': -2}
ROLE_OWNER = ROLES['owner']
ROLE_ADMIN = ROLES['admin']
ROLE_MOD = ROLES['mod']
ROLE_MEMBER = ROLES['member']
ROLE_GUEST = ROLES['guest']
ROLE_MUTED = ROLES['muted']

TYPE_TOPIC = 1
TYPE_JOURNAL = 2
TYPE_COUNCIL = 3

COMMANDS = [
    # community
    'updateSettings', 'subscribe', 'unsubscribe',
    # community+account
    'setRole', 'setUserTitle',
    # community+account+permlink
    'mutePost', 'unmutePost', 'pinPost', 'unpinPost', 'flagPost',
]


def process_json_community_op(actor, op_json, date):
    """Validates community op and apply state changes to db."""
    op = CommunityOp(actor, date)
    op.validate(op_json)
    op.process()

def read_key_str(op, key):
    """Reads a key from a dict, ensuring non-blank str if present."""
    if key in op:
        assert isinstance(op[key], str), 'key `%s` was not str' % key
        assert op[key], 'key `%s` was blank' % key
        return op[key]
    return None

def read_key_json(obj, key):
    """Given a dict, parse JSON in `key`. Blank dict on failure."""
    ret = {}
    if key in obj:
        try:
            ret = json.loads(obj[key])
        except Exception:
            pass
        assert ret, 'json key `%s` was blank' % key
    return ret


class Community:
    """Handles hive community registration and operations."""

    @classmethod
    def register(cls, names, block_date):
        """Block processing: hooks into new account registration.

        `Accounts` calls this method with any newly registered names.
        This method checks for any valid community names and inserts them.
        """

        for name in names:
            if not re.match(r'^hive-[123]\d{4,6}$', name):
                continue
            type_id = int(name[5])
            _id = Accounts.get_id(name)

            # TODO: settings
            sql = """INSERT INTO hive_communities (id, name, title, settings,
                                                   type_id, created_at)
                          VALUES (:id, :name, '', '{}', :type_id, :date)"""
            DB.query(sql, id=_id, name=name, type_id=type_id, date=block_date)
            sql = """INSERT INTO hive_roles (community_id, account_id, role_id, created_at)
                         VALUES (:community_id, :account_id, :role_id, :date)"""
            DB.query(sql, community_id=_id, account_id=_id, role_id=ROLE_OWNER, date=block_date)

    @classmethod
    def validated_name(cls, name):
        """Perform basic validation on community name, then search for id."""
        if (name[:5] == 'hive-'
                and name[5] in ['1', '2', '3']
                and re.match(r'^hive-[123]\d{4,6}$', name)):
            return name
        return None

    @classmethod
    def exists(cls, name):
        """Check if a given community name exists."""
        sql = "SELECT 1 FROM hive_communities WHERE name = :name"
        return bool(DB.query_one(sql, name=name))

    @classmethod
    def get_id(cls, name):
        """Given a community name, get its internal id."""
        sql = "SELECT id FROM hive_communities WHERE name = :name"
        return DB.query_one(sql, name=name)

    @classmethod
    def get_all_muted(cls, community):
        """Return a list of all muted accounts."""
        return DB.query_col("""SELECT name FROM hive_accounts
                                WHERE id IN (SELECT account_id FROM hive_roles
                                              WHERE community_id = :community_id
                                                AND role_id < 0)""",
                            community_id=cls.get_id(community))

    @classmethod
    def get_user_role(cls, community_id, account_id):
        """Get user role within a specific community."""

        return DB.query_one("""SELECT role_id FROM hive_roles
                                WHERE community_id = :community_id
                                  AND account_id = :account_id
                                LIMIT 1""",
                            community_id=community_id,
                            account_id=account_id) or ROLE_GUEST

    @classmethod
    def is_post_valid(cls, community, comment_op: dict):
        """ Given a new post/comment, check if valid as per community rules

        For a comment to be valid, these conditions apply:
            - Author is not muted in this community
            - For council post/comment, author must be a member
            - For journal post, author must be a member
        """

        community_id = cls.get_id(community)
        account_id = Accounts.get_id(comment_op['author'])
        role = cls.get_user_role(community_id, account_id)
        type_id = int(community[5])

        # TODO: (1.5) check that beneficiaries are valid

        if type_id == TYPE_JOURNAL:
            if not comment_op['parent_author']:
                return role >= ROLE_MEMBER
        elif type_id == TYPE_COUNCIL:
            return role >= ROLE_MEMBER
        return role >= ROLE_GUEST # or at least not muted

    @classmethod
    def is_subscribed(cls, community_id, account_id):
        """Check an account's subscription status."""
        sql = """SELECT 1 FROM hive_subscriptions
                  WHERE community_id = :community_id
                    AND account_id = :account_id"""
        return bool(DB.query_one(sql, community_id=community_id,
                                 account_id=account_id))
    @classmethod
    def is_pinned(cls, post_id):
        """Check a post's pinned status."""
        sql = """SELECT is_pinned FROM hive_posts WHERE id = :id"""
        return bool(DB.query_one(sql, id=post_id))

    @classmethod
    def recalc_pending_payouts(cls):
        """Update all hive_community.pending_payout entries."""
        # TODO: use/filter on community field
        sql = """SELECT category, SUM(payout) FROM hive_posts_cache
                  WHERE is_paidout = '0' GROUP BY category
               ORDER BY SUM(payout) DESC"""
        rows = DB.query_all(sql)
        for community, total in rows:
            sql = """UPDATE hive_communities SET pending_payout = :total
                      WHERE name = :community"""
            DB.query(sql, community=community, total=total)

class CommunityOp:
    """Handles validating and processing of community custom_json ops."""
    #pylint: disable=too-many-instance-attributes

    SCHEMA = {
        'setRole': ['community', 'account', 'role'],
        'updateSettings': ['community', 'settings'],
        'setUserTitle': ['community', 'title'],
        'mutePost': ['community', 'account', 'permlink', 'notes'],
        'unmutePost': ['community', 'account', 'permlink', 'notes'],
        'pinPost': ['community', 'account', 'permlink'],
        'unpinPost': ['community', 'account', 'permlink'],
        'flagPost': ['community', 'account', 'permlink', 'notes'],
        'subscribe': ['community'],
        'unsubscribe': ['community'],
    }

    def __init__(self, actor, date):
        """Inits a community op for validation and processing."""
        self.date = date
        self.valid = False
        self.action = None
        self.op = None

        self.actor = actor
        self.actor_id = None

        self.community = None
        self.community_id = None

        self.account = None
        self.account_id = None

        self.permlink = None
        self.post_id = None

        self.role = None
        self.role_id = None

        self.notes = None
        self.title = None
        self.settings = None

    def validate(self, raw_op):
        """Pre-processing and validation of custom_json payload."""
        # validate basic structure
        self._validate_raw_op(raw_op)
        self.action = raw_op[0]
        self.op = raw_op[1]
        self.actor_id = Accounts.get_id(self.actor)

        # validate and read schema
        self._read_schema()

        # validate permissions
        self._validate_permissions()


        self.valid = True

    def process(self):
        """Applies a validated operation."""
        assert self.valid, 'cannot apply invalid op'
        action = self.action
        params = dict(
            date=self.date,
            community=self.community,
            community_id=self.community_id,
            actor=self.actor,
            actor_id=self.actor_id,
            account=self.account,
            account_id=self.account_id,
            post_id=self.post_id,
            role_id=self.role_id,
            notes=self.notes,
            title=self.title,
            settings=json.dumps(self.settings) if self.settings else None
        )


        # Community-level commands
        if action == 'updateSettings':
            DB.query("""UPDATE hive_communities SET settings = :settings
                         WHERE name = :community""", **params)
        elif action == 'subscribe':
            DB.query("""INSERT INTO hive_subscriptions (account_id, community_id)
                        VALUES (:actor_id, :community_id)""", **params)
            DB.query("""UPDATE hive_communities
                           SET subscribers = subscribers + 1
                         WHERE id = :community_id)""", **params)
        elif action == 'unsubscribe':
            DB.query("""DELETE FROM hive_subscriptions
                         WHERE account_id = :actor_id
                           AND community_id = :community_id""", **params)
            DB.query("""UPDATE hive_communities
                           SET subscribers = subscribers - 1
                         WHERE id = :community_id)""", **params)

        # Account-level actions
        elif action == 'setRole':
            DB.query("""UPDATE hive_roles SET role_id = :role_id
                         WHERE account_id = :account_id
                           AND community_id = :community_id""", **params)
        elif action == 'setUserTitle':
            DB.query("""UPDATE hive_roles SET title = :title
                         WHERE account_id = :account_id
                           AND community_id = :community_id""", **params)

        # Post-level actions
        elif action == 'mutePost':
            DB.query("""UPDATE hive_posts SET is_muted = 1
                         WHERE id = :post_id""", **params)
        elif action == 'unmutePost':
            DB.query("""UPDATE hive_posts SET is_muted = 0
                         WHERE id = :post_id""", **params)
        elif action == 'pinPost':
            DB.query("""UPDATE hive_posts SET is_pinned = 1
                         WHERE id = :post_id""", **params)
        elif action == 'unpinPost':
            DB.query("""UPDATE hive_posts SET is_pinned = 0
                         WHERE id = :post_id""", **params)
        elif action == 'flagPost':
            DB.query("""INSERT INTO hive_flags (account, community,
                               author, permlink, comment, created_at)
                        VALUES (:actor, :community, :account, :permlink,
                                :notes, :date)""", **params)

        # INSERT INTO hive_modlog (account, community, action, created_at)
        # VALUES  (account, community, json.inspect, block_date)
        return True


    def _validate_raw_op(self, raw_op):
        assert isinstance(raw_op, list), 'op json must be list'
        assert len(raw_op) == 2, 'op json must have 2 elements'
        assert isinstance(raw_op[0], str), 'op json[0] must be string'
        assert isinstance(raw_op[1], dict), 'op json[1] must be dict'
        assert raw_op[0] in self.SCHEMA.keys(), 'invalid action'
        return (raw_op[0], raw_op[1])

    def _read_schema(self):
        schema = self.SCHEMA[self.action]

        # validate structure
        _keys = self.op.keys()
        missing = schema - _keys
        assert not missing, 'missing keys: %s' % missing
        extra = _keys - schema
        assert not extra, 'extraneous keys: %s' % extra

        # read and validate keys
        if 'community' in schema:
            self._read_community()
        if 'account' in schema:
            self._read_account()
        if 'permlink' in schema:
            self._read_permlink()
        if 'role' in schema:
            self._read_role()
        if 'notes' in schema:
            self._read_notes()
        if 'title' in schema:
            self._read_title()
        if 'settings' in schema:
            self._read_settings()

    def _read_community(self):
        _name = read_key_str(self.op, 'community')
        assert _name, 'must name a community'
        assert Accounts.exists(_name), 'invalid name `%s`' % _name
        _id = Community.get_id(_name)
        assert _id, 'community `%s` does not exist' % _name

        self.community = _name
        self.community_id = _id

    def _read_account(self):
        _name = read_key_str(self.op, 'account')
        assert _name, 'must name an account'
        assert Accounts.exists(_name), 'account `%s` not found' % _name
        self.account = _name
        self.account_id = Accounts.get_id(_name)

    def _read_permlink(self):
        assert self.account, 'permlink requires named account'
        _permlink = read_key_str(self.op, 'permlink')
        assert _permlink, 'must name a permlink'

        from hive.indexer.posts import Posts
        _pid = Posts.get_id(self.account, _permlink)
        assert _pid, 'invalid post: %s/%s' % (self.account, _permlink)

        sql = """SELECT community FROM hive_posts WHERE id = :id LIMIT 1"""
        _comm = DB.query_one(sql, id=_pid)
        assert self.community == _comm, 'post does not belong to community'

        self.permlink = _permlink
        self.post_id = _pid

    def _read_role(self):
        _role = read_key_str(self.op, 'role')
        assert _role, 'must name a role'
        assert _role in ROLES, 'invalid role'
        self.role = _role
        self.role_id = ROLES[_role]

    def _read_notes(self):
        _notes = read_key_str(self.op, 'notes')
        assert _notes, 'notes must be specified'
        assert len(_notes) <= 120, 'notes must be under 120 characters'
        _notes = _notes.strip()
        assert _notes, 'notes cannot be blank'
        self.notes = _notes

    def _read_title(self):
        _title = read_key_str(self.op, 'title') or ''
        _title = _title.strip()
        assert len(_title) < 32, 'user title must be under 32 characters'
        self.title = _title

    def _read_settings(self):
        _settings = read_key_json(self.op, 'settings')
        # TODO: validation
        self.settings = dict(
            title=read_key_str(_settings, 'title'), # 32
            about=read_key_str(_settings, 'about'), #120
            description=read_key_str(_settings, 'description'), #5000
            flag_text=read_key_str(_settings, 'flag_text'), #???
            language=read_key_str(_settings, 'language'), #2 https://en.wikipedia.org/wiki/ISO_639-1
            nsfw=read_key_str(_settings, 'nsfw'), # true/false
            bg_color=read_key_str(_settings, 'bg_color'), #hex
            bg_color2=read_key_str(_settings, 'bg_color2'), #hex
            primary_tag=read_key_str(_settings, 'primary_tag'), #tag (32chars?)
        )

    def _validate_permissions(self):
        community_id = self.community_id
        action = self.action
        actor_role = Community.get_user_role(community_id, self.actor_id)
        new_role = self.role_id

        if action == 'setRole':
            assert actor_role >= ROLE_MOD, 'only mods and up can alter roles'
            assert actor_role > new_role, 'cannot promote to or above own rank'
            if self.actor != self.account:
                account_role = Community.get_user_role(community_id, self.account_id)
                assert account_role < actor_role, 'cant modify higher-role user'
                assert account_role != new_role, 'role would not change'
        elif action == 'updateSettings':
            assert actor_role >= ROLE_ADMIN, 'only admins can update settings'
        elif action == 'setUserTitle':
            assert actor_role >= ROLE_MOD, 'only mods can set user titles'
        elif action == 'mutePost':
            assert actor_role >= ROLE_MOD, 'only mods can mute posts'
        elif action == 'unmutePost':
            assert actor_role >= ROLE_MOD, 'only mods can unmute posts'
        elif action == 'pinPost':
            pinned = Community.is_pinned(self.post_id)
            assert not pinned, 'post is already pinned'
            assert actor_role >= ROLE_MOD, 'only mods can pin posts'
        elif action == 'unpinPost':
            pinned = Community.is_pinned(self.post_id)
            assert pinned, 'post is already not pinned'
            assert actor_role >= ROLE_MOD, 'only mods can unpin posts'
        elif action == 'flagPost':
            # TODO: assert user has not yet flagged post
            assert actor_role > ROLE_MUTED, 'muted users cannot flag posts'
        elif action == 'subscribe':
            active = Community.is_subscribed(community_id, self.actor_id)
            assert not active, 'already subscribed'
        elif action == 'unsubscribe':
            active = Community.is_subscribed(community_id, self.actor_id)
            assert active, 'already unsubscribed'
