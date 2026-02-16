import winston from 'winston';

const LOG_LEVEL = process.env.LOG_LEVEL || 'info';
const LOG_FORMAT = process.env.LOG_FORMAT || 'json';
const NODE_ENV = process.env.NODE_ENV || 'development';

const formats = [
    winston.format.timestamp(),
    winston.format.errors({ stack: true }),
];

if (LOG_FORMAT === 'json') {
    formats.push(winston.format.json());
} else {
    formats.push(
        winston.format.colorize(),
        winston.format.printf(({ timestamp, level, message, ...meta }) => {
            const metaStr = Object.keys(meta).length ? ` ${JSON.stringify(meta)}` : '';
            return `[${timestamp}] ${level}: ${message}${metaStr}`;
        }),
    );
}

export const logger = winston.createLogger({
    level: LOG_LEVEL,
    format: winston.format.combine(...formats),
    defaultMeta: { service: 'gateway' },
    transports: [
        new winston.transports.Console(),
    ],
});

// Add file transport in production
if (NODE_ENV === 'production') {
    const logPath = process.env.LOG_FILE_PATH || './logs';
    logger.add(new winston.transports.File({
        filename: `${logPath}/gateway-error.log`,
        level: 'error',
    }));
    logger.add(new winston.transports.File({
        filename: `${logPath}/gateway-combined.log`,
    }));
}
