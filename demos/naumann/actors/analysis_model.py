from improv.actor import Actor, Spike, RunManager
from improv.store import ObjectNotFoundError
from queue import Empty
import numpy as np
import time
import cv2
import colorsys
import scipy

import logging; logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class ModelAnalysis(Actor):
    #TODO: Add additional error handling
    def __init__(self, *args):
        super().__init__(*args)
        N = 20
        w = np.zeros((N,N)) #guess XX neurons initially?
        h = np.zeros((N,4)) #dh is 4
        k = np.zeros((N,8))
        b = np.zeros(N)
        self.theta = np.concatenate((w,h,b,k), axis=None).flatten()
        self.p = {'numNeurons': N, 'hist_dim': 4, 'numSamples': 1, 'dt': 0.5, 'stim_dim': 8} #TODO: from config file..

    def setup(self, param_file=None):
        '''
        '''
        np.seterr(divide='ignore')

        # TODO: same as behaviorAcquisition, need number of stimuli here. Make adaptive later
        self.num_stim = 21 
        self.frame = 0
        # self.curr_stim = 0 #start with zeroth stim unless signaled otherwise
        self.stim = {}
        self.stimStart = {}
        self.window = 500 #TODO: make user input, choose scrolling window for Visual
        self.C = None
        self.S = None
        self.Call = None
        self.Cx = None
        self.Cpop = None
        self.coords = None
        self.color = None
        self.runMean = None
        self.runMeanOn = None
        self.runMeanOff = None
        self.lastOnOff = None
        self.recentStim = [0]*self.window
        self.currStimID = np.zeros((8, 100000)) #FIXME
        self.currStim = -10
        self.allStims = {}

    def run(self):
        self.total_times = []
        self.puttime = []
        self.colortime = []
        self.stimtime = []
        self.timestamp = []
        self.LL = []

        with RunManager(self.name, self.runStep, self.setup, self.q_sig, self.q_comm) as rm:
            logger.info(rm)
        
        print('Analysis broke, avg time per frame: ', np.mean(self.total_times, axis=0))
        print('Analysis broke, avg time per put analysis: ', np.mean(self.puttime))
        print('Analysis broke, avg time per color frame: ', np.mean(self.colortime))
        print('Analysis broke, avg time per stim avg: ', np.mean(self.stimtime))
        print('Analysis got through ', self.frame, ' frames')

        N = self.p["numNeurons"]
        np.savetxt('output/model_weights.txt', self.theta[:N*N].reshape((N,N)))

        np.savetxt('output/timing/analysis_frame_time.txt', np.array(self.total_times))
        np.savetxt('output/timing/analysis_timestamp.txt', np.array(self.timestamp))
        np.savetxt('output/analysis_estsAvg.txt', np.array(self.estsAvg))
        np.savetxt('output/analysis_proc_S.txt', np.array(self.S))
        np.savetxt('output/analysis_LL.txt', np.array(self.LL))
        
        np.savetxt('output_snap/stims.txt', self.currStimID)

    def runStep(self):
        ''' Take numpy estimates and frame_number
            Create X and Y for plotting
        '''
        t = time.time()
        ids = None
        try: 
            sig = self.links['input_stim_queue'].get(timeout=0.0001)
            self.updateStim_start(sig)
        except Empty as e:
            pass #no change in input stimulus
        try:
            ids = self.q_in.get(timeout=0.0001)
            if ids is not None and ids[0]==1:
                print('analysis: missing frame')
                self.total_times.append(time.time()-t)
                self.q_out.put([1])
                raise Empty
            # t = time.time()
            self.frame = ids[-1]
            (self.coordDict, self.image, self.S) = self.client.getList(ids[:-1])
            self.C = self.S
            self.coords = [o['coordinates'] for o in self.coordDict]
            
            # Compute tuning curves based on input stimulus
            # Just do overall average activity for now
            self.stimAvg_start()

            # fit to model once we have enough neurons
            if self.C.shape[0]>=self.p['numNeurons']:
                self.fit()
            
            self.globalAvg = np.mean(self.estsAvg[:,:8], axis=0)
            self.tune = [self.estsAvg[:,:8], self.globalAvg]
        
            # N  = self.p['numNeurons']
            # dh = self.p['hist_dim']
            # ds = self.p['stim_dim']
            # k = np.square(self.theta[N*(N+dh+1):].reshape((N, ds))*1e6)
            # self.globalAvg = np.mean(k, axis=0)
            # self.tune_k = [k, self.globalAvg]

            # Compute coloring of neurons for processed frame
            # Also rotate and stack as needed for plotting
            # TODO: move to viz, but we don't need to compute this 30 times/sec
            self.color = self.plotColorFrame()

            if self.frame >= self.window:
                window = self.window
                self.Cx = np.arange(self.frame-window,self.frame)
            else:
                window = self.frame
                self.Cx = np.arange(0,self.frame)

            if self.C.shape[1]>0:
                self.Cpop = np.nanmean(self.C, axis=0)
                self.Call = self.C #already a windowed version #[:,self.frame-window:self.frame]
            
            ## for figures only
            if self.frame in [200, 500, 1000, 2000, 2875]:
                # save weights
                N = self.p["numNeurons"]
                np.savetxt('output_snap/model_weights_frame'+str(self.frame)+'.txt', self.theta[:N*N].reshape((N,N)))
                # save calcium traces
                np.savetxt('output_snap/ests_C_frame'+str(self.frame)+'.txt', self.C)
                # save computed tuning curves
                np.savetxt('output_snap/tuning_curves_frame'+str(self.frame)+'.txt', self.estsAvg)

            self.putAnalysis()
            self.timestamp.append([time.time(), self.frame])
            self.total_times.append(time.time()-t)
        except ObjectNotFoundError:
            logger.error('Estimates unavailable from store, droppping')
        except Empty as e:
            pass
        except Exception as e:
            logger.exception('Error in analysis: {}'.format(e))
    
    def fit(self):
        '''
        '''        
        if self.p["numNeurons"] < self.S.shape[0]: #check for more neurons
            self.updateTheta()

        self.p["numSamples"] = self.frame

        # print('First test: ', self.ll(self.S[:,:self.frame]))

        if self.frame<50:
            y_step = self.S[:,:self.frame]
            stim_step = self.currStimID[:, :self.frame]
        else:
            y_step =  self.S[:,self.frame-50:self.frame]
            stim_step = self.currStimID[:, self.frame-50:self.frame]

        y_step = np.where(np.isnan(y_step), 0, y_step) #Why are there nans here anyway?? #TODO

        t0 = time.time()
        self.theta -= 1e-5*self.ll_grad(y_step, stim_step)
        self.LL.append(self.ll(y_step, stim_step))
        # print(time.time()-t0, self.p['numNeurons'], self.LL[-1])

        # gradStep = self.j_ll_grad(self.theta, y_step, self.p)
        # self.theta -= 1e-5*gradStep
        # self.theta -= 1e-5 * self.j_ll_grad(self.theta, y_step, self.p)

    def ll(self, y, s):
        '''
        log-likelihood objective and gradient
        '''
        # get parameters
        dh = self.p['hist_dim']
        ds = self.p['stim_dim']
        dt = self.p['dt']
        N  = self.p['numNeurons']
        # M  = self.p['numSamples']
        eps = np.finfo(float).eps

        # run model at theta
        data = {}
        data['y'] = y
        data['s'] = s
        rhat = self.runModel(data)
        # try:
        #     rhat = rhat*dt
        # except FloatingPointError:
        #     print('FPE in rhat*dt; likely underflow')

        # model parameters
        # w = self.theta[:N*N].reshape((N,N))
        # h = self.theta[N*N:N*(N+dh)].reshape((N,dh))
        # b = self.theta[N*(N+dh):].reshape(N)

        # compute negative log-likelihood
        # include l1 or l2 penalty on weights
        # l2 = scipy.linalg.norm(w) #100*np.sqrt(np.sum(np.square(theta['w'])))
        # l1 = np.sum(np.sum(np.abs(w)))/(N*N)

        ll_val = ((np.sum(rhat) - np.sum(y*np.log(rhat+eps))) )/(y.shape[1]*N**2)  #+ l1
        # if np.isnan(ll_val):
        #     logger.error('------------ nans in LL')

        return ll_val

    def ll_grad(self, y, s):
        # get parameters
        dh = self.p['hist_dim']
        dt = self.p['dt']
        N  = self.p['numNeurons']
        M  = y.shape[1] #params['numSamples'] #TODO: should be equal

        # run model at theta
        data = {}
        data['y'] = y
        data['s'] = s
        rhat = self.runModel(data)  #TODO: rewrite without dicts
        # rhat = rhat*dt

        # compute gradient
        grad = dict()

        # difference in computed rate vs. observed spike count
        rateDiff = (rhat - data['y'])

        # graident for baseline
        grad['b'] = np.sum(rateDiff, axis=1)/M

        # gradient for stim
        grad['k'] = rateDiff.dot(data['s'].T)/M

        # gradient for coupling terms
        yr = np.roll(data['y'], 1)
        #yr[0,:] = 0
        grad['w'] = rateDiff.dot(yr.T)/M #+ 2*np.abs(self.theta[:N*N].reshape((N,N)))/(N*N)
        #self.d_abs(self.theta[:N*N].reshape((N,N)))
        np.fill_diagonal(grad['w'], 0)
        
        # gradient for history terms
        grad['h'] = np.zeros((N,dh))
        #grad['h'][:,0] = rateDiff[:,0].dot(data['y'][:,0].T)/M
        for i in np.arange(0,N):
            for j in np.arange(0,dh):
                grad['h'][i,j] = np.sum(np.flip(data['y'],1)[i,:]*rateDiff[i,:])/M

        # check for nans
        # grad = self.gradCheck(grad)

        # flatten grad
        grad_flat = np.concatenate((grad['w'],grad['h'],grad['b'],grad['k']), axis=None).flatten()/N

        return grad_flat

    # def gradCheck(self, grad):
    #     resgrad = {}
    #     for key in grad.keys():
    #         resgrad[key] = self.arrayCheck(grad[key], key)
    #     return resgrad

    # def arrayCheck(self, arr, name):
    #     if ~np.isfinite(arr).all():
    #         print('**** WARNING: Found non-finite value in ' + name + ' (%g percent of the values were bad)'%(np.mean(np.isfinite(arr))))
    #     arr = np.where(np.isnan(arr), 0, arr)
    #     arr[arr == np.inf] = 0
    #     return arr

    def d_abs(self, weights):
        pos = (weights>=0)*1
        neg = (weights<0)*-1
        return pos+neg

    def runModel(self, data):
        '''
        Generates the output of the model given some theta
        Returns data dict like generateData()
        '''

        # constants
        dh = self.p['hist_dim']
        N  = self.p['numNeurons']

        # nonlinearity (exp)
        f = np.exp

        expo = np.zeros((N,data['y'].shape[1]))
        # simulate the model for t samples (time steps)
        for j in np.arange(0,data['y'].shape[1]): 
            expo[:,j] = self.runModelStep(data['y'][:,j-dh:j], data['s'][:,j])
        
        # computed rates
        # try:
        rates = f(expo)
        # except:
        #     import pdb; pdb.set_trace()

        return rates

    def runModelStep(self, y, s):
        ''' Runs the model forward one point in time
            y should contain only up to t-dh:t points per neuron
        '''
        # constants
        N  = self.p['numNeurons']
        dh = self.p['hist_dim']
        ds = self.p['stim_dim']

        # model parameters
        w = self.theta[:N*N].reshape((N,N))
        h = self.theta[N*N:N*(N+dh)].reshape((N,dh))
        b = self.theta[N*(N+dh):N*(N+dh+1)].reshape(N)
        k = self.theta[N*(N+dh+1):].reshape((N, ds))

        # data length in time
        t = y.shape[1]

        expo = np.zeros(N)
        for i in np.arange(0,N): # step through neurons
            ## NOTE: brief benchmarking showed this faster than matrix ops
            # compute model firing rate
            if t<1:
                hist = 0
            else:
                hist = np.sum(np.flip(h[i,:])*y[i,:])
                
            if t>0:
                w[i,i] = 0 #diagonals zero
                weights = w[i,:].dot(y[:,-1])
            else:
                weights = 0

            stim = k[i,:].dot(s) #np.square(k[i,:]).dot(s)
            
            expo[i] = (b[i] + hist + weights + stim) #+ eps #remove log 0 errors
        
        return expo

    def updateTheta(self):
        ''' TODO: Currently terribly inefficient growth
            Probably initialize large and index into it however many N we have
        '''
        N  = self.p['numNeurons']
        dh = self.p['hist_dim']
        ds = self.p['stim_dim']

        old_w = self.theta[:N*N].reshape((N,N))
        old_h = self.theta[N*N:N*(N+dh)].reshape((N,dh))
        old_b = self.theta[N*(N+dh):N*(N+dh+1)].reshape(N)
        old_k = self.theta[N*(N+dh+1):].reshape((N, ds))

        self.p["numNeurons"] = self.S.shape[0] #confirm this
        M  = self.p['numNeurons']

        w = np.zeros((M,M))
        w[:N, :N] = old_w
        h = np.zeros((M,dh))
        h[:N, :] = old_h
        b = np.zeros(M)
        b[:N] = old_b
        k = np.zeros((M,ds))
        k[:N, :] = old_k

        self.theta = np.concatenate((w,h,b,k), axis=None).flatten()


    def updateStim_start(self, stim):
        frame = list(stim.keys())[0]
        whichStim = stim[frame][0]
        stimID = self.IDstim(int(whichStim))
        if whichStim not in self.stimStart.keys():
            self.stimStart.update({whichStim:[]})
            self.allStims.update({stimID:[]})
        if abs(stim[frame][1])>1 :
            curStim = 1 #on
            self.allStims[stimID].append(frame)
        else:
            curStim = 0 #off
        if self.lastOnOff is None:
            self.lastOnOff = curStim
        elif self.lastOnOff == 0 and curStim == 1: #was off, now on
            self.stimStart[whichStim].append(frame)
            print('Stim ', whichStim, ' started at ', frame)
            if stimID > -10:
                self.currStimID[stimID, frame] = 1
            self.currStim = stimID
        else:
            # stim off
            self.currStimID[:, frame] = np.zeros(8)

        #TODO: only works if stim and frame received concurrently, which they are for this demo

        self.lastOnOff = curStim

    def IDstim(self, s):
        stim = -10
        if s == 3:
            stim = 0
        elif s==10:
            stim = 1
        elif s==9:
            stim = 2
        elif s==16:
            stim = 3
        elif s==4:
            stim = 4
        elif s==14:
            stim = 5
        elif s==13:
            stim = 6
        elif s==12:
            stim = 7

        return stim

    def putAnalysis(self):
        ''' Throw things to DS and put IDs in queue for Visual
        '''
        t = time.time()
        ids = []
        stim = [self.lastOnOff, self.currStim]
        N  = self.p['numNeurons']
        w = self.theta[:N*N].reshape((N,N))

        ids.append(self.client.put(self.Cx, 'Cx'+str(self.frame)))
        ids.append(self.client.put(self.Call, 'Call'+str(self.frame)))
        ids.append(self.client.put(self.Cpop, 'Cpop'+str(self.frame)))
        ids.append(self.client.put(self.tune, 'tune'+str(self.frame)))
        ids.append(self.client.put(self.color, 'color'+str(self.frame)))
        ids.append(self.client.put(self.coordDict, 'analys_coords'+str(self.frame)))
        ids.append(self.client.put(self.allStims, 'stim'+str(self.frame)))
        ids.append(self.client.put(w, 'w'+str(self.frame)))
        ids.append(self.client.put(np.array(self.LL), 'LL'+str(self.frame)))
        ids.append(self.frame)

        self.q_out.put(ids)
        self.puttime.append(time.time()-t)

    def stimAvg_start(self):
        ests = self.S #ests = self.C
        ests_num = ests.shape[1]
        t = time.time()
        polarAvg = [np.zeros(ests.shape[0])]*12
        estsAvg = [np.zeros(ests.shape[0])]*self.num_stim
        for s,l in self.stimStart.items():
            l = np.array(l)
            # print('stim ', s, ' is l ', l)
            if l.size>0:
                onInd = np.array([np.arange(o+4,o+18) for o in np.nditer(l)]).flatten()
                onInd = onInd[onInd<ests_num]
                # print('on ', onInd)
                offInd = np.array([np.arange(o-10,o+25) for o in np.nditer(l)]).flatten() #TODO replace
                offInd = offInd[offInd>=0]
                offInd = offInd[offInd<ests_num]
                # print('off ', offInd)
                try:
                    if onInd.size>0:
                        onEst = np.mean(ests[:,onInd], axis=1)
                    else:
                        onEst = np.zeros(ests.shape[0])
                    if offInd.size>0:
                        offEst = np.mean(ests[:,offInd], axis=1)
                    else:
                        offEst = np.zeros(ests.shape[0])
                    try:
                        estsAvg[int(s)] = onEst - offEst #(onEst / offEst) - 1
                    except FloatingPointError:
                        print('Could not compute on/off: ', onEst, offEst)
                        estsAvg[int(s)] = onEst
                    except ZeroDivisionError:
                        estsAvg[int(s)] = np.zeros(ests.shape[0])
                except FloatingPointError: #IndexError:
                    logger.error('Index error ')
                    print('int s is ', int(s))
            # else:
            #     estsAvg[int(s)] = np.zeros(ests.shape[0])

        estsAvg = np.array(estsAvg)
        #TODO: efficient update, no copy
        polarAvg[2] = estsAvg[9,:] #np.sum(estsAvg[[9,11,15],:], axis=0)
        polarAvg[1] = estsAvg[10, :]
        polarAvg[0] = estsAvg[3, :] #np.sum(estsAvg[[3,5,8],:], axis=0)
        polarAvg[7] = estsAvg[12, :]
        polarAvg[6] = estsAvg[13, :] #np.sum(estsAvg[[13,17,18],:], axis=0)
        polarAvg[5] = estsAvg[14, :]
        polarAvg[4] = estsAvg[4, :] #np.sum(estsAvg[[4,6,7],:], axis=0)
        polarAvg[3] = estsAvg[16, :]

        # for color summation
        polarAvg[8] = estsAvg[5, :]
        polarAvg[9] = estsAvg[6, :]
        polarAvg[10] = estsAvg[7, :]
        polarAvg[11] = estsAvg[8, :]
        
        self.estsAvg = np.abs(np.transpose(np.array(polarAvg)))
        self.estsAvg = np.where(np.isnan(self.estsAvg), 0, self.estsAvg)
        self.estsAvg[self.estsAvg == np.inf] = 0
        #self.estsAvg = np.clip(self.estsAvg*4, 0, 4)
        self.stimtime.append(time.time()-t)

    def plotColorFrame(self):
        ''' Computes colored nicer background+components frame
        '''
        t = time.time()
        image = self.image
        color = np.stack([image, image, image, image], axis=-1).astype(np.uint8).copy()
        color[...,3] = 255
            # color = self.color.copy() #TODO: don't stack image each time?
        if self.coords is not None:
            for i,c in enumerate(self.coords):
                #c = np.array(c)
                ind = c[~np.isnan(c).any(axis=1)].astype(int)
                #TODO: Compute all colors simultaneously! then index in...
                cv2.fillConvexPoly(color, ind, self._tuningColor(i, color[ind[:,1], ind[:,0]]))

        # TODO: keep list of neural colors. Compute tuning colors and IF NEW, fill ConvexPoly. 

        self.colortime.append(time.time()-t)
        return color

    def _tuningColor(self, ind, inten):
        ''' ind identifies the neuron by number
        '''
        ests = self.estsAvg
        #ests = self.tune_k[0] 
        if ests[ind] is not None: # and np.sum(np.abs(self.estsAvg[ind]))>2:
            try:
                # trying sort and compare
                # rel = self.estsAvg[ind]/np.max(self.estsAvg[ind])
                # order = np.argsort(rel)
                # if rel[order[-1]] - rel[order[-2]] > 0.2: #ensure strongish tuning
                    # r, g, b = self.manual_Color(order[-1])
                    # return (r, g, b) + (255,)
                # else:
                #     # print(order, rel)
                #     return (255, 255, 255, 50)

                # trying summation coloring
                return self.manual_Color_Sum(ests[ind])

                # scale by intensity
                # tc = np.nanargmax(self.estsAvg[ind])
                # r, g, b,  = self.manual_Color_Sum(self.estsAvg[ind]) #self.manual_Color(tc)
                # # h = (np.nanargmax(self.estsAvg[ind])*45)/360
                # intensity = 1 #- np.mean(inten[0][0])/255.0
                # # r, g, b, = colorsys.hls_to_rgb(h, intensity, 1)
                # # r, g, b = [x*255.0 for x in (r, g, b)]
                # return (r, g, b) + (intensity*255,)
                
            except ValueError:
                return (255,255,255,0)
            except Exception:
                print('inten is ', inten)
                print('ests[i] is ', ests[ind])
        else:
            return (255,255,255,50) #0)

    def manual_Color_Sum_k(self, x):
        ''' x should be length 8 array for coloring
        '''

        mat_weight = np.array([
            [1, 0.25, 0],
            [0.75, 1, 0],
            [0, 1, 0],
            [0, 0.75, 1],
            [0, 0.25, 1],
            [0.25, 0, 1.],
            [1, 0, 1],
            [1, 0, 0.25],
        ])

        color = x @ mat_weight

        blend = 0.8  #0.3
        thresh = 0.2   #0.1
        thresh_max = blend * np.max(color)

        color = np.clip(color, thresh, thresh_max)
        color -= thresh
        color /= thresh_max
        color = np.nan_to_num(color)

        if color.any() and np.linalg.norm(color-np.ones(3))>0.35:
            # print('color is ', color, 'with distance', np.linalg.norm(color-np.ones(3)))
            color *=255
            return (color[0], color[1], color[2], 255)       
        else:
            return (255, 255, 255, 10)

    def manual_Color_Sum(self, x):
        ''' x should be length 12 array for coloring
        '''

        mat_weight = np.array([
            [1, 0.25, 0],
            [0.75, 1, 0],
            [0, 2, 0],
            [0, 0.75, 1],
            [0, 0.25, 1],
            [0.25, 0, 1.],
            [1, 0, 1],
            [1, 0, 0.25],
            [1, 0, 0],
            [0, 0, 1],
            [0, 0, 1],
            [1, 0, 0]
        ])

        color = x @ mat_weight

        blend = 0.8  #0.3
        thresh = 0.2   #0.1
        thresh_max = blend * np.max(color)

        color = np.clip(color, thresh, thresh_max)
        color -= thresh
        color /= thresh_max
        color = np.nan_to_num(color)

        if color.any() and np.linalg.norm(color-np.ones(3))>0.35:
            # print('color is ', color, 'with distance', np.linalg.norm(color-np.ones(3)))
            color *=255
            return (color[0], color[1], color[2], 255)       
        else:
            return (255, 255, 255, 10)