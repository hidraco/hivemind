import logging
import configargparse

class Conf():
    _args = None

    @classmethod
    def read(cls):
        assert not cls._args, "config already read"

        #pylint: disable=invalid-name,line-too-long
        p = configargparse.get_arg_parser(default_config_files=['./hive.conf'])

        # common
        p.add('--database-url', env_var='DATABASE_URL', required=True, help='database connection url', default='postgresql://user:pass@localhost:5432/hive')
        p.add('--steemd-url', env_var='STEEMD_URL', required=True, help='steemd/jussi endpoint', default='https://api.steemit.com')
        p.add('--log-level', env_var='LOG_LEVEL', default='INFO')

        # specific to indexer
        p.add('--max-workers', type=int, env_var='MAX_WORKERS', default=1)
        p.add('--max-batch', type=int, env_var='MAX_BATCH', default=100)
        p.add('--trail-blocks', type=int, env_var='TRAIL_BLOCKS', default=2)

        # specific to API server
        p.add('--port', type=int, env_var='PORT', default=8080)

        cls._args = p.parse_args()

    @classmethod
    def args(cls):
        return cls._args

    @classmethod
    def get(cls, param):
        assert cls._args, "run Conf.read()"
        return getattr(cls._args, param)

    @classmethod
    def log_level(cls):
        str_log_level = cls.get('log_level')
        log_level = getattr(logging, str_log_level.upper(), None)
        if not isinstance(log_level, int):
            raise ValueError('Invalid log level: %s' % str_log_level)
        return log_level
