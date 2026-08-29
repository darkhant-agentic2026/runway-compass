import * as gcipCloudFunctions from 'gcip-cloud-functions';

import { blockPasswordSignUp } from './blockPasswordSignUp';

const authClient = new gcipCloudFunctions.Auth();

export const beforeCreate = authClient.functions().beforeCreateHandler(blockPasswordSignUp);
