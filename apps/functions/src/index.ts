import { beforeUserCreated } from 'firebase-functions/v2/identity';

import { blockPasswordSignUp } from './blockPasswordSignUp';

export const beforeCreate = beforeUserCreated(blockPasswordSignUp);
